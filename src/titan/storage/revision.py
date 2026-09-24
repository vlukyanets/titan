"""The startup check: refuse a database older than the code (database-migrations.md)."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

import titan.migrations

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(titan.migrations.__file__).resolve().parent


class SchemaTooOldError(RuntimeError):
    pass


async def database_revision(conn: AsyncConnection) -> str | None:
    exists = await conn.scalar(text("select to_regclass('alembic_version') is not null"))
    if not exists:
        return None
    value = await conn.scalar(text("select version_num from alembic_version"))
    return str(value) if value is not None else None


def is_older(revision: str | None, script: ScriptDirectory | None = None) -> bool:
    """Whether the database at `revision` lacks migrations this code needs.

    A revision the code does not know is newer: expand/contract keeps it
    compatible, so it is accepted.
    """
    if revision is None:
        return True
    script = script or ScriptDirectory(str(MIGRATIONS_DIR))
    if revision in script.get_heads():
        return False
    try:
        known = script.get_revision(revision)
    except CommandError:
        known = None
    if known is None:
        log.warning("database revision %s is newer than this code", revision)
        return False
    return True


async def check_revision(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        revision = await database_revision(conn)
    if is_older(revision):
        raise SchemaTooOldError(
            f"the database schema ({revision or 'empty'}) is older than this code needs; "
            "run `titan migrate` on one node first"
        )
