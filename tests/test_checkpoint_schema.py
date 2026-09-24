"""The checkpoint tables Alembic creates match what the LangGraph saver expects."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.postgres.base import MIGRATIONS
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

from titan.storage.checkpoints import SAVER_MIGRATIONS

TABLES = ("checkpoint_migrations", "checkpoints", "checkpoint_blobs", "checkpoint_writes")
REFERENCE = "langgraph_reference"


def test_the_revision_covers_every_saver_migration() -> None:
    assert len(MIGRATIONS) == SAVER_MIGRATIONS, (
        "langgraph-checkpoint-postgres changed its schema: add an Alembic revision "
        "that applies the new migrations, then update SAVER_MIGRATIONS"
    )


def conninfo(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


async def describe(conn: psycopg.AsyncConnection[Any], schema: str) -> dict[str, Any]:
    columns = await conn.execute(
        "SELECT table_name, column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns WHERE table_schema = %s AND table_name = ANY(%s) "
        "ORDER BY table_name, ordinal_position",
        (schema, list(TABLES)),
    )
    keys = await conn.execute(
        "SELECT tc.table_name, array_agg(kcu.column_name::text ORDER BY kcu.ordinal_position) "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema "
        "WHERE tc.table_schema = %s AND tc.table_name = ANY(%s) "
        "AND tc.constraint_type = 'PRIMARY KEY' GROUP BY tc.table_name ORDER BY tc.table_name",
        (schema, list(TABLES)),
    )
    indexes = await conn.execute(
        "SELECT tablename, indexname, replace(indexdef, %s, '') FROM pg_indexes "
        "WHERE schemaname = %s AND tablename = ANY(%s) "
        "AND indexname NOT LIKE '%%pkey' AND indexname NOT LIKE 'pk_%%' ORDER BY indexname",
        (f"{schema}.", schema, list(TABLES)),
    )
    return {
        "columns": await columns.fetchall(),
        "keys": await keys.fetchall(),
        "indexes": await indexes.fetchall(),
    }


@pytest.mark.db
async def test_alembic_tables_match_saver_setup(db_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(conninfo(db_url), autocommit=True) as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {REFERENCE} CASCADE")
        await conn.execute(f"CREATE SCHEMA {REFERENCE}")
        try:
            await conn.execute(f"SET search_path TO {REFERENCE}")
            await AsyncPostgresSaver(conn).setup()  # type: ignore[arg-type]
            await conn.execute("SET search_path TO public")
            assert await describe(conn, "public") == await describe(conn, REFERENCE)
        finally:
            await conn.execute(f"DROP SCHEMA {REFERENCE} CASCADE")


@pytest.mark.db
async def test_setup_has_nothing_left_to_do(db_url: str) -> None:
    async with await psycopg.AsyncConnection.connect(
        conninfo(db_url), autocommit=True, row_factory=dict_row
    ) as conn:
        before = await describe(conn, "public")
        await AsyncPostgresSaver(conn).setup()
        assert await describe(conn, "public") == before
        cursor = await conn.execute("SELECT max(v) AS v FROM checkpoint_migrations")
        assert await cursor.fetchone() == {"v": SAVER_MIGRATIONS - 1}
