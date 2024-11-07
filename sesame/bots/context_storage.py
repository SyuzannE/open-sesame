import asyncio
import uuid
from copy import deepcopy
from typing import Any, List, Literal

from common.models import Message
from deepdiff import DeepDiff as compare
from loguru import logger
from pipecat.frames.frames import EndFrame, Frame, TransportMessageUrgentFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.openai import OpenAILLMContext, OpenAILLMContextFrame
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


class RTVIItemStoredMessageData(BaseModel):
    action: Literal["append", "replace"]
    items: List[Any]


class RTVIItemStoredMessage(BaseModel):
    label: Literal["rtvi-ai"] = "rtvi-ai"
    type: Literal["bot-tts-text"] = "storage-item-stored"
    id: str
    data: RTVIItemStoredMessageData


class PersistentContextProcessor(FrameProcessor):
    def __init__(self, storage, *, push_transport_message_upstream: bool = False):
        super().__init__()
        self._storage = storage
        self._push_transport_message_direction = (
            FrameDirection.UPSTREAM
            if push_transport_message_upstream
            else FrameDirection.DOWNSTREAM
        )
        self._register_event_handler("endframe")

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, OpenAILLMContextFrame):
            action, id, items = await self._storage.save(frame.context)
            await self._push_transport_save_message(action, id, items)
        elif isinstance(frame, EndFrame):
            await self._call_event_handler("endframe")

        await self.push_frame(frame, direction)

    async def _push_transport_save_message(self, action, id, items):
        message = RTVIItemStoredMessage(
            id=id,
            data=RTVIItemStoredMessageData(action=action, items=items),
        )
        frame = TransportMessageUrgentFrame(message=message.model_dump(exclude_none=True))
        await self.push_frame(frame, self._push_transport_message_direction)


class PersistentContextStorage:
    def __init__(
        self,
        *,
        db: AsyncSession,
        conversation_id: uuid.UUID,
        language_code: str = "english",
        context: OpenAILLMContext = None,
    ):
        self._db = db
        self._conversation_id = conversation_id
        self._language_code = language_code
        self._messages = deepcopy(context.messages) if context else []
        self._queue = asyncio.Queue()
        self._worker_task = asyncio.create_task(self._worker())
        self._running = True

    def create_processor(
        self, *, exit_on_endframe: bool = False, push_transport_message_upstream: bool = False
    ):
        fp = PersistentContextProcessor(
            self, push_transport_message_upstream=push_transport_message_upstream
        )
        if exit_on_endframe:
            fp.add_event_handler("endframe", self.close)
        return fp

    async def save(self, context: OpenAILLMContext):
        return_action = None
        return_items = None

        messages = context.messages

        if len(messages) >= len(self._messages) and compare(
            messages[: len(self._messages)], self._messages
        ).to_dict():
            additional_messages = messages[len(self._messages) :]
            await self._queue.put(("append", additional_messages))
            return_action = "append"
            return_items = additional_messages
        else:
            await self._queue.put(("replace", messages))
            return_action = "replace"
            return_items = messages

        self._messages = deepcopy(messages)
        return (return_action, str(len(self._messages)), return_items)

    async def _worker(self):
        while self._running:
            try:
                operation, messages = await self._queue.get()
                try:
                    if operation == "append":
                        await self._db_append_messages(messages)
                    elif operation == "replace":
                        await self._db_replace_messages(messages)
                except Exception as e:
                    logger.error(f"Database operation failed: {e}")
                finally:
                    self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in worker: {e}")

    async def _db_append_messages(self, additional_messages):
        await self.store_messages(additional_messages)

    async def _db_replace_messages(self, messages):
        await self.store_messages(messages, erase_before_store=True)

    async def close(self, processor):
        logger.debug("Closing PersistentContextStorage")
        self._running = False
        await self._queue.join()
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass

    async def store_messages(self, context_messages, erase_before_store=False):
        try:
            ms = []
            for i, message_data in enumerate(context_messages, start=1):
                m = Message(
                    conversation_id=self._conversation_id,
                    content=message_data,
                    language_code=message_data.get("language_code", "english"),
                )
                ms.append(m)

            if erase_before_store:
                # Fetch the lowest numbered message in the conversation
                result = await self._db.execute(
                    select(Message)
                    .where(Message.conversation_id == self._conversation_id)
                    .order_by(Message.message_number)
                    .limit(1)
                )
                lowest_message = result.scalar_one_or_none()

                message_id_to_keep = None
                if lowest_message and lowest_message.content.get("role") == "system":
                    message_id_to_keep = lowest_message.message_id

                # Delete all messages for this conversation except for the system message (if it exists)
                delete_query = Message.__table__.delete().where(
                    Message.conversation_id == self._conversation_id
                )
                if message_id_to_keep:
                    delete_query = delete_query.where(Message.message_id != message_id_to_keep)

                await self._db.execute(delete_query)

            # Add the new messages to the session and flush
            self._db.add_all(ms)

            await self._db.flush()

            logger.debug(f"Stored {len(context_messages)} messages")

        except IntegrityError:
            # If we get an integrity error, it means another process inserted a message
            # with the same number. Roll back and try again.
            logger.debug("Integrity error storing messages, rolling back")
            await self._db.rollback()

        except Exception as e:
            logger.debug(f"Error storing messages: {e}")
            # For any other error, rollback and re-raise
            await self._db.rollback()
            raise e
