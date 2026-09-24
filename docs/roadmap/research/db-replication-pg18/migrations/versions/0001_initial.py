"""Initial spike schema

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

from migrations.flavour import flavour

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    if flavour() != "cockroach":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("external_key", sa.String(64), unique=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("attrs", JSONB(), nullable=False),
        sa.Column("origin", sa.String(8), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("batch", sa.String(32), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(16)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("fired_at", sa.DateTime(timezone=True)),
        sa.Column("fired_by", sa.String(16)),
    )
    op.create_index("ix_jobs_batch", "jobs", ["batch"])
    op.create_table(
        "embeddings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("chunk", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("embeddings")
    op.drop_table("jobs")
    op.drop_table("tasks")
