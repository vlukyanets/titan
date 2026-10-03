"""added embeddings of notes and memories

Revision ID: 28bf86f1609b
Revises: 63c947cf3a90
Create Date: 2026-10-02 16:34:23.588936
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "28bf86f1609b"
down_revision: str | None = "63c947cf3a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector ships with the database image (ADR 0006).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "embeddings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("note_id", sa.Uuid(), nullable=True),
        sa.Column("memory_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("content_hash", sa.String(length=32), nullable=False),
        # No dimension, so changing the model needs no migration (ADR 0014).
        sa.Column("vector", Vector(), nullable=False),
        sa.CheckConstraint(
            "(note_id IS NULL) <> (memory_id IS NULL)", name=op.f("ck_embeddings_one_item")
        ),
        sa.ForeignKeyConstraint(
            ["memory_id"],
            ["memories.id"],
            name=op.f("fk_embeddings_memory_id_memories"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["note_id"], ["notes.id"], name=op.f("fk_embeddings_note_id_notes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embeddings")),
    )
    op.create_index(
        "ix_embeddings_memory_id_model_chunk",
        "embeddings",
        ["memory_id", "model", "chunk_index"],
        unique=True,
    )
    op.create_index(
        "ix_embeddings_note_id_model_chunk",
        "embeddings",
        ["note_id", "model", "chunk_index"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_embeddings_note_id_model_chunk", table_name="embeddings")
    op.drop_index("ix_embeddings_memory_id_model_chunk", table_name="embeddings")
    op.drop_table("embeddings")
    # The extension stays: dropping it needs every vector column gone on every
    # node, and an unused extension costs nothing.
