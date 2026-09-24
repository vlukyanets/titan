"""Engine and session factories."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from titan.settings import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    # Creating the engine does not connect; the first query does.
    return create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
