"""Alembic env for a cr-sqlite node.

Every connection loads cr-sqlite and sqlite-vec, and calls crsql_finalize()
before it closes (cr-sqlite requires this). render_as_batch=True so that
ALTERs SQLite cannot do natively go through Alembic's batch (copy-and-move) mode.
"""
import os
import sqlite3

import sqlite_vec
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, event, pool

DB_PATH = os.environ.get("TITAN_DB_PATH", "/data/titan.db")
CRSQLITE = os.environ.get("CRSQLITE_PATH", "/opt/ext/crsqlite")
if context.config.config_file_name:
    fileConfig(context.config.config_file_name)


def make_engine():
    eng = create_engine(f"sqlite:///{DB_PATH}", poolclass=pool.NullPool)

    @event.listens_for(eng, "connect")
    def _load(dbapi_conn, _rec):
        dbapi_conn.enable_load_extension(True)
        dbapi_conn.load_extension(CRSQLITE)
        sqlite_vec.load(dbapi_conn)
        dbapi_conn.execute("PRAGMA busy_timeout=30000")

    @event.listens_for(eng, "close")
    def _finalize(dbapi_conn, _rec):
        dbapi_conn.execute("SELECT crsql_finalize()")

    return eng


def run_migrations_online():
    with make_engine().connect() as connection:
        context.configure(connection=connection, target_metadata=None, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline mode not supported in the spike")
run_migrations_online()
