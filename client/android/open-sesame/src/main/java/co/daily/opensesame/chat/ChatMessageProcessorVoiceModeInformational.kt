package co.daily.opensesame.chat

import ai.rtvi.client.transport.MsgServerToClient
import ai.rtvi.client.types.Transcript
import androidx.compose.runtime.Immutable
import co.daily.opensesame.MessageRole

@Immutable
class ChatMessageProcessorVoiceModeInformational(
    private val helper: ChatMessageProcessorHelper
) : ChatMessageProcessor {

    override fun onUserStartedSpeaking() {
        helper.finalizeMessage(MessageRole.User, replacementText = null)
    }

    override fun onUserTranscript(data: Transcript) {
        if (data.final) {
            helper.appendOrCreateMessage(
                role = MessageRole.User,
                text = data.text,
                transcriptionFinal = true
            )
        }
    }

    override fun onBotLLMStarted() {
        helper.finalizeMessage(
            role = MessageRole.Assistant,
            replacementText = null,
        )
    }

    override fun onBotLLMText(data: MsgServerToClient.Data.BotLLMTextData) {
        helper.appendOrCreateMessage(
            role = MessageRole.Assistant,
            text = data.text,
            transcriptionFinal = true
        )
    }
}