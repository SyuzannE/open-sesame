import asyncio
import base64
import functools
import os
import re
import secrets
import shutil
import string
import subprocess
from pathlib import Path
from typing import Callable, Dict, Literal, Optional

import typer
from argon2 import PasswordHasher
from common.database import construct_database_url
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.status import Status
from rich.syntax import Syntax
from rich.table import Table
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import uvicorn

console = Console()
app = typer.Typer(
    help="Sesame CLI - Development and management tools for the Sesame application.",
    no_args_is_help=True,
    add_completion=False,
) 

env_example = Path("env.example")
env_file = Path(".env")
schema_file = Path("../schema/")


def check_required_env_vars() -> bool:
    """Check if all required environment variables are present in .env."""
    # Variables that must have non-empty values
    required_vars = [
        "SESAME_APP_SECRET",
        "SESAME_DATABASE_ADMIN_USER",
        "SESAME_DATABASE_NAME",
        "SESAME_DATABASE_HOST",
        "SESAME_DATABASE_PORT",
        "SESAME_DATABASE_USER",
    ]

    # Variables that must exist but can be empty
    required_vars_allow_empty = ["SESAME_DATABASE_ADMIN_PASSWORD", "SESAME_DATABASE_PASSWORD"]

    if not env_file.exists():
        console.print("\n✗ No .env file found. Please run 'sesame init' first.", style="red bold")
        return False

    load_dotenv(env_file)

    # Check vars that must have values
    missing_vars = [var for var in required_vars if not os.getenv(var)]

    # Check vars that must exist but can be empty
    undefined_vars = [var for var in required_vars_allow_empty if os.getenv(var) is None]

    if missing_vars or undefined_vars:
        console.print("\n✗ Missing required environment variables:", style="red bold")
        for var in missing_vars:
            console.print(f"  • {var}", style="red")
        for var in undefined_vars:
            console.print(f"  • {var} (can be empty but must be defined)", style="red")
        console.print(
            "\nPlease run 'sesame init' or 'sesame init-db' to set these variables.", style="yellow"
        )
        return False

    return True


def require_env(f: Callable) -> Callable:
    """Decorator to ensure environment variables."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not check_required_env_vars():
            raise typer.Exit(1)
        return f(*args, **kwargs)

    return wrapper


def require_env_and_schema(f: Callable) -> Callable:
    """Decorator to ensure environment variables and schema exist before running a command."""

    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not check_required_env_vars():
            raise typer.Exit(1)
        if not check_schema_exists():
            raise typer.Exit(1)
        return f(*args, **kwargs)

    return wrapper


def construct_admin_database_url() -> str:
    """Construct database URL using admin credentials."""
    db_url = (
        f"{os.getenv('SESAME_DATABASE_PROTOCOL', 'postgresql')}+"
        f"{os.getenv('SESAME_DATABASE_ASYNC_DRIVER', 'asyncpg')}://"
        f"{os.getenv('SESAME_DATABASE_ADMIN_USER', 'postgres')}:"
        f"{os.getenv('SESAME_DATABASE_ADMIN_PASSWORD', '')}@"
        f"{os.getenv('SESAME_DATABASE_HOST', 'localhost')}:"
        f"{os.getenv('SESAME_DATABASE_PORT', '5432')}/"
        f"{os.getenv('SESAME_DATABASE_NAME', 'sesame')}"
    )
    return db_url


def format_env_contents(current_contents: list[str], updates: Dict[str, str]) -> list[str]:
    """Format the environment contents with updates."""
    env_contents = current_contents.copy()

    # Update each variable
    for var_name, var_value in updates.items():
        var_found = False
        for i, line in enumerate(env_contents):
            if line.strip().startswith(f"{var_name}="):
                env_contents[i] = f'{var_name}="{var_value}"\n'
                var_found = True
                break

        if not var_found:
            env_contents.append(f'{var_name}="{var_value}"\n')

    return env_contents


def handle_env_updates(
    env_updates: Dict[str, str],
    action: Literal["print", "save", "both", "skip"] = "both",
    init_mode: bool = False,
) -> None:
    """Helper function to handle env variable updates with flexible output options.

    Args:
        env_updates: Dictionary of environment variables to update
        action: How to handle the updates ("print", "save", "both", or "skip")
        init_mode: If True, creates a fresh .env file instead of updating existing one
    """
    try:
        if init_mode:
            # For initialization, always start with the example template
            with open(env_example, "r") as f:
                current_contents = f.readlines()

            # If .env exists, create a backup before overwriting
            if env_file.exists():
                backup_file = env_file.with_suffix(".backup")
                shutil.copy2(env_file, backup_file)
                console.print(f"Created backup of existing .env at {backup_file}", style="yellow")
        else:
            # For updates, use existing .env or fall back to example
            if env_file.exists():
                with open(env_file, "r") as f:
                    current_contents = f.readlines()
            else:
                with open(env_example, "r") as f:
                    current_contents = f.readlines()
                console.print("\nNo .env file found, using env.example as template", style="yellow")

        # Format the updated contents
        updated_contents = format_env_contents(current_contents, env_updates)
        formatted_text = "".join(updated_contents)

        # Handle based on specified action
        if action in ["print", "both"]:
            console.print("\nUpdated environment variables:", style="blue bold")
            syntax = Syntax(
                formatted_text, "env", theme="monokai", line_numbers=False, word_wrap=True
            )
            console.print(syntax)

        if action in ["save", "both"]:
            # Write the updates
            with open(env_file, "w") as f:
                f.writelines(updated_contents)
            console.print(
                "✓ Created new .env file" if init_mode else "✓ Successfully updated .env file",
                style="bold green",
            )

        if action == "skip":
            console.print("Skipped environment variable updates", style="yellow")

    except Exception as e:
        console.print(f"\nError handling .env file: {str(e)}", style="red")
        raise typer.Exit(1)


def generate_secret() -> str:
    """Generate a secure random secret using base64."""
    random_bytes = secrets.token_bytes(32)
    return base64.b64encode(random_bytes).decode("utf-8")


def generate_user_id(length: int = 32) -> str:
    """Generate a secure random user ID using only alphanumeric characters."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_db_password(length: int = 32) -> str:
    """Generate a secure random password safe for database use.
    Only includes letters and numbers to avoid SQL escaping issues."""
    alphabet = string.ascii_letters + string.digits
    password = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
    ]
    password.extend(secrets.choice(alphabet) for _ in range(length - 3))
    password_list = list(password)
    secrets.SystemRandom().shuffle(password_list)
    return "".join(password_list)


def split_sql_statements(sql: str) -> list[str]:
    """Split SQL into individual statements, handling dollar-quoted blocks and comments."""
    statements = []
    current_statement = []
    in_dollar_quote = False
    dollar_quote_tag = ""

    lines = sql.split("\n")

    for line in lines:
        stripped_line = line.strip()

        # Skip empty lines
        if not stripped_line:
            continue

        # Handle single line comments
        if stripped_line.startswith("--"):
            continue

        # Handle dollar quoting
        if not in_dollar_quote:
            # Look for start of dollar quote
            match = re.match(r".*(\$[^$]*\$)", line)
            if match:
                in_dollar_quote = True
                dollar_quote_tag = match.group(1)
        else:
            # Look for matching end dollar quote
            if dollar_quote_tag in line:
                in_dollar_quote = False
                dollar_quote_tag = ""

        current_statement.append(line)

        # If we're not in a dollar quote, check for statement end
        if not in_dollar_quote and ";" in line:
            full_statement = "\n".join(current_statement).strip()
            if full_statement:  # Only add non-empty statements
                # Remove any trailing comments after the semicolon
                statement_parts = full_statement.split(";")
                clean_statement = ";".join(statement_parts[:-1]) + ";"
                if clean_statement.strip() != ";":  # Don't add standalone semicolons
                    statements.append(clean_statement)
            current_statement = []

    # Add any remaining statement that's not empty or just comments
    remaining_statement = "\n".join(current_statement).strip()
    if remaining_statement and not remaining_statement.startswith("--"):
        statements.append(remaining_statement)

    # Return only non-empty, non-comment statements
    return [stmt for stmt in statements if stmt.strip() and not stmt.strip().startswith("--")]


async def check_schema_exists() -> bool:
    """Check if the schema has been applied by verifying the users table exists."""
    admin_url = construct_admin_database_url()
    admin_engine = create_async_engine(
        admin_url,
        echo=bool(int(os.getenv("SESAME_DATABASE_ECHO_OUTPUT", "0"))),
    )

    try:
        async with admin_engine.begin() as conn:
            result = await conn.execute(
                text("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_name = 'users'
                    )
                """)
            )
            exists = bool(result.scalar())
            if not exists:
                console.print(
                    "\n✗ Database schema not found. Please run 'sesame run-schema' first.",
                    style="red bold",
                )
            return exists
    finally:
        await admin_engine.dispose()


# ========================
# Initialize
# ========================


@app.command()
def init():
    """Initialize a new project and Open Sesame environment."""

    if env_file.exists():
        warning_panel = Panel(
            "[yellow]A project .env file already exists.\n"
            "Proceeding will create a fresh .env file.\n"
            "Your current .env will be backed up to .env.backup[/yellow]",
            title="[red bold]Warning",
            border_style="red",
        )
        console.print("\n", warning_panel, "\n")

        if not Confirm.ask("Would you like to proceed?"):
            raise typer.Exit()

    if Confirm.ask("\nWould you like to generate a random app secret? (SESAME_APP_SECRET)"):
        app_secret = generate_secret()
        console.print("Generated random app secret.")
    else:
        app_secret = typer.prompt(
            "Please enter your app secret",
            hide_input=True,
        )
        console.print("Using provided app secret.")

    table = Table(show_header=False, box=None, padding=(0, 2), collapse_padding=True)

    table.add_column("Option", style="cyan")
    table.add_column("Description", style="white")

    table.add_row("print", "Display the changes in the terminal without saving to .env")
    table.add_row("save", "Save the changes to .env without displaying")
    table.add_row("both", "Display and save the changes")
    table.add_row("skip", "Cancel without making any changes")

    console.print("\nAvailable options:", style="blue bold")
    console.print(table)
    console.print()

    action = Prompt.ask(
        "How would you like to handle these updates?",
        choices=["print", "save", "both", "skip"],
        default="both",
    )

    handle_env_updates({"SESAME_APP_SECRET": app_secret}, action=action, init_mode=True)

    if Confirm.ask("\nWould you like to configure your database?"):
        init_db()
    else:
        console.print("Skipping database configuration. You can do this later by running init_db")

    console.print("\nProject successfully initialized!", style="green bold")


# ========================
# Initialize database
# ========================


@app.command()
def init_db():
    """Initialize the Open Sesame database configuration."""

    if not env_file.exists():
        console.print("\nNo .env file found. Please run 'init' first.", style="red")
        raise typer.Exit(1)

    console.print("\nDatabase Configuration", style="blue bold")
    console.print("Please provide the following database details:\n")

    db_engine = Prompt.ask(
        "Which database would you like to use?",
        choices=["postgresql"],
        default="postgresql",
    )

    # Collect database configuration
    admin_user = Prompt.ask("Database admin username", default="postgres")

    default_app_user = "sesame"
    namespace_found = None

    # Split on dot and take the last part
    parts = admin_user.split(".")
    if len(parts) > 1:
        namespace_found = parts[-1]
        default_app_user = f"sesame.{namespace_found}"
        console.print(
            f"[dim]Detected namespace '[cyan]{namespace_found}[/cyan]' "
            f"from admin user, suggesting application user: [cyan]{default_app_user}[/cyan][/dim]"
        )

    # Collect database configuration
    db_config = {
        "SESAME_DATABASE_PROTOCOL": db_engine,
        "SESAME_DATABASE_ADMIN_USER": admin_user,
        "SESAME_DATABASE_ADMIN_PASSWORD": Prompt.ask("Database admin password", password=True),
        "SESAME_DATABASE_NAME": Prompt.ask("Database name", default="sesame"),
        "SESAME_DATABASE_HOST": Prompt.ask("Database host", default="localhost"),
        "SESAME_DATABASE_PORT": Prompt.ask("Database port", default="5432"),
        "SESAME_DATABASE_USER": Prompt.ask(
            "Application database user (this will be created)", default=default_app_user
        ),
    }

    # Handle the application database password
    if Confirm.ask(
        "\nWould you like to generate a random password for the application database user?"
    ):
        db_password = generate_db_password()
        console.print(
            "\nGenerated random database password (using only letters and numbers for compatibility).",
            style="green",
        )
    else:
        while True:
            db_password = Prompt.ask("Enter password for application database user", password=True)
            confirm_password = Prompt.ask("Confirm password", password=True)

            if db_password == confirm_password:
                break
            console.print("Passwords don't match. Please try again.", style="red")

    db_config["SESAME_DATABASE_PASSWORD"] = db_password

    # Show summary before updating
    console.print("\nDatabase Configuration Summary:", style="blue bold")
    for key, value in db_config.items():
        if "PASSWORD" in key:
            display_value = "********"
        else:
            display_value = value
        console.print(f"{key}: {display_value}")

    table = Table(show_header=False, box=None, padding=(0, 2), collapse_padding=True)

    table.add_column("Option", style="cyan")
    table.add_column("Description", style="white")

    table.add_row("print", "Display the changes in the terminal without saving to .env")
    table.add_row("save", "Save the changes to .env without displaying")
    table.add_row("both", "Display and save the changes")
    table.add_row("skip", "Cancel without making any changes")

    console.print("\nAvailable options:", style="blue bold")
    console.print(table)
    console.print()

    action = Prompt.ask(
        "\nHow would you like to handle these updates?",
        choices=["print", "save", "both", "skip"],
        default="both",
    )

    handle_env_updates(db_config, action=action)

    if action != "skip":
        console.print("\nDatabase configuration completed successfully!", style="green bold")
    else:
        console.print("\nDatabase configuration cancelled.", style="yellow")

    if Confirm.ask("\nWould you like to test your database credentials?"):
        test_db(as_admin=True)
    else:
        console.print("Skipping database tests. You can do this later by running test_db")

    console.print("Database successfully initialized!", style="green bold")


# ========================
# Test Database Connection
# ========================


@app.command()
@require_env
def test_db(
    as_admin: bool = typer.Option(False, "--admin", help="Test with admin / superuser role"),
):
    """Test database connection using async engine."""
    try:
        load_dotenv(env_file)
        asyncio.run(_test_db(as_admin))
    except Exception as e:
        console.print(f"\nError connecting to database: {str(e)}", style="red bold")
        raise typer.Exit(1)


async def _test_db(as_admin=True):
    """Async function to test database connection."""

    if as_admin:
        console.print("Running Database Connection Test (Admin Role)", style="blue bold")
        # Set up the admin engine
        admin_url = construct_admin_database_url()
        engine_for_role = create_async_engine(
            admin_url,
            echo=bool(int(os.getenv("SESAME_DATABASE_ECHO_OUTPUT", "0"))),
        )
    else:
        console.print("Running Database Connection Test (User Role)", style="blue bold")
        user_url = construct_database_url()
        engine_for_role = create_async_engine(
            user_url,
            echo=bool(int(os.getenv("SESAME_DATABASE_ECHO_OUTPUT", "0"))),
        )

    console.print("\nDatabase Connection Test", style="blue bold")

    with Status("[blue]Testing database connection...", spinner="dots"):
        try:
            async with engine_for_role.begin() as conn:
                result = await conn.execute(text("SELECT 1"))
                result.scalar()

            console.print("\n✓ Successfully connected to database!", style="green bold")

            async with engine_for_role.begin() as conn:
                result = await conn.execute(text("SELECT version()"))
                version = result.scalar()
                console.print(f"Database version: {version}", style="dim")

        except Exception as e:
            console.print("\n✗ Failed to connect to database", style="red bold")
            console.print(f"Error: {str(e)}", style="red")
            raise


# ========================
# Run DB Schema
# ========================


@app.command()
@require_env
def run_schema():
    """Apply database schema using existing configuration."""
    try:
        load_dotenv(env_file)
        asyncio.run(_run_schema())
    except Exception as e:
        console.print(f"\nError applying schema: {str(e)}", style="red bold")
        raise typer.Exit(1)


def validate_schema_replacements(schema_content: str, replacements: Dict[str, str]) -> None:
    """Validate that all required placeholders have valid replacements."""
    for placeholder, value in replacements.items():
        if not value:
            raise ValueError(f"Replacement value for {placeholder} cannot be None or empty")
        if placeholder not in schema_content:
            console.print(f"Warning: Placeholder {placeholder} not found in schema", style="yellow")


async def _run_schema():
    """Async function to apply database schema."""
    await _test_db(as_admin=True)

    _schema_file = schema_file / "postgresql.sql"

    # Ensure schema file exists
    if not _schema_file.exists():
        raise FileNotFoundError(f"Schema file not found at {_schema_file}")

    # Read schema content
    with open(_schema_file, "r") as f:
        schema_content = f.read()

    # Ensure we have a schema to work with
    if not schema_content.strip():
        raise ValueError("Schema file is empty")

    # Set defaults and ensure they're not None
    sesame_user = os.getenv("SESAME_DATABASE_USER", "sesame")
    if not sesame_user:
        sesame_user = "sesame"
        console.print("Warning: Using default user 'sesame'", style="yellow")

    sesame_password = os.getenv("SESAME_DATABASE_PASSWORD")
    if not sesame_password:
        sesame_password = generate_db_password()
        console.print("Generated new database password", style="yellow")

    # Validate replacements before proceeding
    replacements = {"%%PASSWORD%%": sesame_password, "%%USER%%": sesame_user.split(".")[0]}
    validate_schema_replacements(schema_content, replacements)

    # Set up the admin engine
    admin_url = construct_admin_database_url()
    admin_engine = create_async_engine(
        admin_url,
        echo=bool(int(os.getenv("SESAME_DATABASE_ECHO_OUTPUT", "0"))),
    )

    with Status("[blue]Applying database schema...", spinner="dots"):
        # Replace placeholders in schema
        schema_sql = schema_content
        for placeholder, value in replacements.items():
            schema_sql = schema_sql.replace(placeholder, value)

        # Validate the resulting SQL
        if not schema_sql.strip():
            raise ValueError("Generated SQL is empty after replacements")

        # Split and execute the statements
        statements = split_sql_statements(schema_sql)
        valid_statements = [stmt for stmt in statements if stmt.strip()]

        if not valid_statements:
            raise ValueError("No valid SQL statements found in schema")

        console.print(f"\nExecuting {len(statements)} SQL statements...", style="blue")

        async with admin_engine.begin() as conn:
            for i, statement in enumerate(statements, 1):
                if statement.strip():
                    try:
                        await conn.execute(text(statement))
                    except Exception:
                        console.print(f"\n✗ Error in statement {i}:", style="red bold")
                        console.print(statement, style="red")
                        raise

        console.print("\n✓ Schema successfully applied!", style="green bold")

        # Get the created role name
        created_role = None
        async with admin_engine.begin() as conn:
            result = await conn.execute(
                text(
                    f"SELECT rolname FROM pg_roles WHERE rolname LIKE '{sesame_user}%' ORDER BY rolname DESC LIMIT 1"
                )
            )
            row = result.first()  # Don't await this, first() returns the Row directly
            if row:
                created_role = row[0]

        if not created_role:
            created_role = sesame_user

        # Create the output table
        table = Table(
            box=box.ROUNDED, show_header=False, show_edge=False, pad_edge=False, style="green"
        )

        table.add_column("Key", style="green dim")
        table.add_column("Value", style="green bold")

        table.add_row("SESAME_DATABASE_USER", f'"{created_role}"')
        table.add_row("SESAME_DATABASE_PASSWORD", f'"{sesame_password}"')

        # Create a panel containing the table
        panel = Panel(
            table,
            title="[yellow]Check your sesame/.env includes these values:",
            title_align="left",
            subtitle="[dim]Copy these values to your .env file",
            subtitle_align="left",
            box=box.ROUNDED,
            border_style="blue",
            padding=(1, 2),
        )

        console.print("\n")
        console.print(panel)
        console.print("\n")


# ========================
# Create User
# ========================


@app.command()
@require_env_and_schema
def create_user(
    username: str = typer.Option(None, "--username", "-u", help="Username for the new user"),
    password: str = typer.Option(None, "--password", "-p", help="Password for the new user"),
):
    """Create a new user with secure password hashing."""

    try:
        # Load environment variables
        load_dotenv(env_file)

        if not os.getenv("SESAME_APP_SECRET"):
            console.print(
                "SESAME_APP_SECRET missing from .env, required to salt encrypted passwords. Please set this and try again.",
                style="red bold",
            )
            raise typer.Exit(1)

        # Run the async user creation
        asyncio.run(_create_user(username, password))
    except Exception as e:
        console.print(f"Error creating user: {str(e)}", style="red bold")
        raise typer.Exit(1)


async def _create_user(username: Optional[str] = None, password: Optional[str] = None):
    """Async function to create a new user."""
    # Validate database connection first
    await _test_db(as_admin=True)

    console.print("\nUser Creation", style="blue bold")

    # Validate or prompt for username
    if username:
        if len(username) <= 2:
            console.print("Provided username must be more than 2 characters", style="red")
            username = None

    # Prompt for username if not provided or invalid
    while not username:
        username = Prompt.ask("Enter a username")
        if len(username) <= 2:
            console.print("Username must be more than 2 characters", style="red")
            username = None

    # Validate or prompt for password
    if password:
        if len(password) < 8:
            console.print("Provided password must be at least 8 characters", style="red")
            password = None
        else:
            # Confirm the provided password
            password_confirm = Prompt.ask("Confirm your password", password=True)
            if password != password_confirm:
                console.print("Passwords do not match", style="red")
                password = None

    # Prompt for password if not provided, invalid, or confirmation failed
    while not password:
        password = Prompt.ask("Enter a password (min 8 characters)", password=True)
        if len(password) < 8:
            console.print("Password must be at least 8 characters", style="red")
            continue

        password_confirm = Prompt.ask("Confirm your password", password=True)
        if password == password_confirm:
            break
        console.print("Passwords do not match", style="red")

    # Generate user_id and hash password
    user_id = generate_user_id()
    ph = PasswordHasher()
    password_hash = ph.hash(password)

    # Set up the admin engine for database operations
    admin_url = construct_admin_database_url()
    admin_engine = create_async_engine(
        admin_url,
        echo=bool(int(os.getenv("SESAME_DATABASE_ECHO_OUTPUT", "0"))),
    )

    with Status("[blue]Creating user...", spinner="dots") as status:
        try:
            async with admin_engine.begin() as conn:
                # Check if username already exists
                result = await conn.execute(
                    text("SELECT COUNT(*) FROM users WHERE username = :username"),
                    {"username": username},
                )
                count = result.scalar()

                if not count or count > 0:
                    raise ValueError("Username already exists")

                # Insert new user
                await conn.execute(
                    text("""
                        INSERT INTO users (user_id, username, password_hash)
                        VALUES (:user_id, :username, :password_hash)
                    """),
                    {"user_id": user_id, "username": username, "password_hash": password_hash},
                )
            status.stop()
            console.print("\n✓ User successfully created!", style="green bold")
        except Exception as e:
            status.stop()
            console.print("\n✗ Failed to create user", style="red bold")
            console.print(f"Error: {str(e)}", style="red")
            raise
        finally:
            await admin_engine.dispose()

        # Create a nice table for the credentials
        table = Table(
            box=box.ROUNDED, show_header=False, show_edge=False, pad_edge=False, style="green"
        )

        table.add_column("Field", style="green dim")
        table.add_column("Value", style="green bold")

        table.add_row("Username", username)
        table.add_row("Password", password)

        # Create a panel containing the table
        panel = Panel(
            table,
            title="[yellow]Your User Credentials",
            title_align="left",
            subtitle="[red]Please make sure to keep these credentials safe. You will not be able to recover them later.",
            subtitle_align="left",
            box=box.ROUNDED,
            border_style="blue",
            padding=(1, 2),
        )

        console.print("\n")
        console.print(panel)
        console.print("\n")


# ========================
# Run FastAPI App
# ========================


@app.command()
@require_env_and_schema
def run(
    host: str = typer.Option(None, "--host", "-h", help="Bind socket to this host."),
    port: int = typer.Option(None, "--port", "-p", help="Bind socket to this port."),
    reload: bool = typer.Option(True, "--reload/--no-reload", help="Enable auto-reload."),
):
    """Run the FastAPI server using uvicorn."""
    load_dotenv(env_file)

    try:
        final_port = port or int(os.getenv("SESAME_WEBAPP_PORT", "8000"))
        app_path = "webapp.main:app"

        # Build command arguments
        command = [
            "uvicorn",
            app_path,
            "--port",
            str(final_port),
        ]

        if host:
            command.extend(["--host", host])

        if reload:
            command.append("--reload")

        # Show server info
        console.print("\nStarting development server...", style="blue bold")
        console.print(f"Application: {app_path}", style="blue")
        if host:
            console.print(f"Host: {host}", style="blue")
        console.print(f"Port: {final_port}", style="blue")
        console.print(f"Reload: {'enabled' if reload else 'disabled'}", style="blue")
        console.print("\nPress CTRL+C to stop the server\n", style="yellow")

        # Run uvicorn
        subprocess.run(command)

    except KeyboardInterrupt:
        console.print("\nServer stopped", style="yellow")
    except Exception as e:
        console.print(f"\nError starting server: {str(e)}", style="red bold")
        raise typer.Exit(1)


# ========================
# Services
# ========================
@app.command()
def services():
    from common.service_factory import ServiceFactory

    console.print("\nAvailable Services:", style="blue bold")
    console.print(ServiceFactory.get_service_info())


def main():
    app()



if __name__ == "__main__":
    uvicorn.run("webapp.main:app", host="127.0.0.1", port=7860, reload=True)
    