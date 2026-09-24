"""The LangGraph Postgres checkpointer's tables, owned by Alembic.

`AsyncPostgresSaver.setup()` would create these itself, with statements that
Alembic cannot roll out node by node (ADR 0006). The same tables are declared
here and created by a revision instead, and the revision marks every saver
migration as applied so `setup()` has nothing left to do.
`tests/test_checkpoint_schema.py` fails when a LangGraph upgrade changes them.
"""

from __future__ import annotations

from sqlalchemy import Column, Index, Integer, LargeBinary, Table, Text
from sqlalchemy.dialects.postgresql import JSONB

from titan.storage.base import Base

# Number of entries in langgraph.checkpoint.postgres.base.MIGRATIONS that the
# tables below correspond to (langgraph-checkpoint-postgres 3.1.2).
SAVER_MIGRATIONS = 10

checkpoint_migrations = Table(
    "checkpoint_migrations",
    Base.metadata,
    Column("v", Integer, primary_key=True, autoincrement=False),
)

checkpoints = Table(
    "checkpoints",
    Base.metadata,
    Column("thread_id", Text, primary_key=True),
    Column("checkpoint_ns", Text, primary_key=True, server_default=""),
    Column("checkpoint_id", Text, primary_key=True),
    Column("parent_checkpoint_id", Text),
    Column("type", Text),
    Column("checkpoint", JSONB, nullable=False),
    Column("metadata", JSONB, nullable=False, server_default="{}"),
    Index("checkpoints_thread_id_idx", "thread_id"),
)

checkpoint_blobs = Table(
    "checkpoint_blobs",
    Base.metadata,
    Column("thread_id", Text, primary_key=True),
    Column("checkpoint_ns", Text, primary_key=True, server_default=""),
    Column("channel", Text, primary_key=True),
    Column("version", Text, primary_key=True),
    Column("type", Text, nullable=False),
    Column("blob", LargeBinary),
    Index("checkpoint_blobs_thread_id_idx", "thread_id"),
)

checkpoint_writes = Table(
    "checkpoint_writes",
    Base.metadata,
    Column("thread_id", Text, primary_key=True),
    Column("checkpoint_ns", Text, primary_key=True, server_default=""),
    Column("checkpoint_id", Text, primary_key=True),
    Column("task_id", Text, primary_key=True),
    Column("idx", Integer, primary_key=True, autoincrement=False),
    Column("channel", Text, nullable=False),
    Column("type", Text),
    Column("blob", LargeBinary, nullable=False),
    Column("task_path", Text, nullable=False, server_default=""),
    Index("checkpoint_writes_thread_id_idx", "thread_id"),
)
