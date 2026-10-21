"""The data module: the only code that runs SQL."""

import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    delete,
    false,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql.elements import ColumnElement


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    # The hash of the one-time sign-in code; None once it is spent.
    code_hash: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ModelRow(Base):
    __tablename__ = "models"
    __table_args__ = (
        UniqueConstraint("user_id", "name"),
        # At most one current model per user.
        Index(
            "models_one_current_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("current"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str]
    url: Mapped[str]
    # Encrypted with the node key; None for a model that needs no key.
    encrypted_key: Mapped[bytes | None]
    # The model the chat uses.
    current: Mapped[bool] = mapped_column(server_default=false())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MessageRow(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="messages_role"),
        # The chat reads a user's messages in order on every message.
        Index("messages_of_user", "user_id", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str]
    content: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'done')", name="tasks_status"),
        # The chat lists a user's open or done tasks in order.
        Index("tasks_of_user", "user_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str]
    note: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class Model:
    name: str
    url: str
    encrypted_key: bytes | None
    current: bool = False


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    note: str | None
    status: str


def _task(row: TaskRow) -> Task:
    return Task(row.id, row.title, row.note, row.status)


class NameTaken(Exception):
    pass


_session: ContextVar[AsyncSession] = ContextVar("session")

# Random bytes in a sign-in code, which is typed once, and in a token,
# which lives in a file.
CODE_BYTES = 16
TOKEN_BYTES = 32


def make_engine(url: str) -> AsyncEngine:
    # Keeps SQL parameters out of error messages and debug logs.
    return create_async_engine(url, hide_parameters=True)


async def ping(engine: AsyncEngine) -> bool:
    """Whether the database answers a query."""
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError, OSError:
        return False
    return True


@asynccontextmanager
async def transaction(engine: AsyncEngine) -> AsyncIterator[None]:
    """Runs the data functions called inside it in one transaction.

    It commits when the block ends and rolls back when the block raises.
    """
    async with AsyncSession(engine) as session, session.begin():
        reset = _session.set(session)
        try:
            yield
        finally:
            _session.reset(reset)


def _hash(secret: str) -> str:
    # Codes and tokens are long and random, so a fast hash is enough.
    return hashlib.sha256(secret.encode()).hexdigest()


async def add_user(name: str) -> str:
    """Adds a user and returns their one-time sign-in code."""
    session = _session.get()
    code = secrets.token_urlsafe(CODE_BYTES)
    session.add(User(name=name, code_hash=_hash(code)))
    try:
        await session.flush()
    except IntegrityError:
        raise NameTaken(name) from None
    return code


async def sign_in(name: str, code: str) -> str | None:
    """Spends the user's one-time code and returns a new token.

    Returns None when the name and the code do not match, also for a code
    that is already spent.
    """
    session = _session.get()
    # One statement, so two sign-ins with the same code cannot both pass.
    user_id = await session.scalar(
        update(User)
        .where(User.name == name, User.code_hash == _hash(code))
        .values(code_hash=None)
        .returning(User.id)
    )
    if user_id is None:
        return None
    token = secrets.token_urlsafe(TOKEN_BYTES)
    session.add(Token(user_id=user_id, token_hash=_hash(token)))
    await session.flush()
    return token


async def user_of_token(token: str) -> int | None:
    """The id of the user the token belongs to, or None."""
    return await _session.get().scalar(
        select(Token.user_id).where(Token.token_hash == _hash(token))
    )


async def user_name(user_id: int) -> str:
    result = await _session.get().execute(select(User.name).where(User.id == user_id))
    return result.scalar_one()


def _of_user(user_id: int, name: str) -> list[ColumnElement[bool]]:
    return [ModelRow.user_id == user_id, ModelRow.name == name]


async def _lock_models(user_id: int) -> None:
    # Changes to which model is current wait for each other, so two of them
    # at once cannot both make a model current.
    await _session.get().execute(
        select(User.id).where(User.id == user_id).with_for_update()
    )


async def save_model(user_id: int, model: Model) -> None:
    """Adds a model, or replaces the user's model of the same name.

    The user's first model becomes the current one.
    """
    await _lock_models(user_id)
    has_current = (
        select(ModelRow.id)
        .where(ModelRow.user_id == user_id, ModelRow.current)
        .exists()
    )
    values = {"url": model.url, "encrypted_key": model.encrypted_key}
    # One statement, so two saves of one name at once cannot both insert.
    await _session.get().execute(
        insert(ModelRow)
        .values(user_id=user_id, name=model.name, current=~has_current, **values)
        .on_conflict_do_update(
            index_elements=[ModelRow.user_id, ModelRow.name], set_=values
        )
    )


async def models_of(user_id: int) -> list[Model]:
    rows = await _session.get().scalars(
        select(ModelRow).where(ModelRow.user_id == user_id).order_by(ModelRow.name)
    )
    return [Model(r.name, r.url, r.encrypted_key, r.current) for r in rows]


async def remove_model(user_id: int, name: str) -> bool:
    """Removes the user's model; False when they have none of that name."""
    removed = await _session.get().scalar(
        delete(ModelRow).where(*_of_user(user_id, name)).returning(ModelRow.id)
    )
    return removed is not None


async def use_model(user_id: int, name: str) -> bool:
    """Makes the model current; False when the user has none of that name."""
    session = _session.get()
    await _lock_models(user_id)
    if (
        await session.scalar(select(ModelRow.id).where(*_of_user(user_id, name)))
        is None
    ):
        return False
    # Two statements: the index allows one current model at any moment.
    await session.execute(
        update(ModelRow)
        .where(ModelRow.user_id == user_id, ModelRow.current)
        .values(current=False)
    )
    await session.execute(
        update(ModelRow).where(*_of_user(user_id, name)).values(current=True)
    )
    return True


async def current_model(user_id: int) -> Model | None:
    row = await _session.get().scalar(
        select(ModelRow).where(ModelRow.user_id == user_id, ModelRow.current)
    )
    return None if row is None else Model(row.name, row.url, row.encrypted_key, True)


async def messages_of(user_id: int) -> list[Message]:
    """The user's conversation, oldest message first."""
    rows = await _session.get().scalars(
        select(MessageRow).where(MessageRow.user_id == user_id).order_by(MessageRow.id)
    )
    return [Message(r.role, r.content) for r in rows]


async def add_messages(user_id: int, *messages: Message) -> None:
    _session.get().add_all(
        MessageRow(user_id=user_id, role=m.role, content=m.content) for m in messages
    )


async def add_task(user_id: int, title: str, note: str | None) -> Task:
    session = _session.get()
    row = TaskRow(user_id=user_id, title=title, note=note, status="open")
    session.add(row)
    await session.flush()
    return _task(row)


async def tasks_of(user_id: int, status: str) -> list[Task]:
    """The user's tasks of that status, oldest first."""
    rows = await _session.get().scalars(
        select(TaskRow)
        .where(TaskRow.user_id == user_id, TaskRow.status == status)
        .order_by(TaskRow.id)
    )
    return [_task(r) for r in rows]


async def update_task(
    user_id: int, task_id: int, changes: dict[str, str]
) -> Task | None:
    """Changes the user's task; None when they have no task of that id."""
    mine = [TaskRow.id == task_id, TaskRow.user_id == user_id]
    statement = (
        update(TaskRow).where(*mine).values(**changes).returning(TaskRow)
        if changes
        else select(TaskRow).where(*mine)
    )
    row = await _session.get().scalar(statement)
    return None if row is None else _task(row)
