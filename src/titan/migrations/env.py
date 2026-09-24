"""Async Alembic environment.

The URL is taken from the Alembic config when a caller sets one (tests, `titan migrate`),
otherwise from TITAN_DATABASE_URL.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import titan.domains  # registers every domain model on Base.metadata
import titan.scheduler.models  # scheduler leases
import titan.storage.checkpoints  # noqa: F401  # LangGraph checkpoint tables
from titan.settings import Settings
from titan.storage.base import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return Settings().database_url.get_secret_value()


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_online())
