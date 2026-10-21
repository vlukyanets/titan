"""The data module: the only code that runs SQL."""

import hashlib
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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


class NameTaken(Exception):
    pass


_session: ContextVar[AsyncSession] = ContextVar("session")


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
    code = secrets.token_urlsafe(16)
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
    token = secrets.token_urlsafe(32)
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
