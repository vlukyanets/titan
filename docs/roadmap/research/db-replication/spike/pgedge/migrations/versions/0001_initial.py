"""initial schema: tasks, jobs, embeddings

Revision ID: 0001_initial
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

TSTZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    # pgvector must exist on every node; with Spock auto-DDL this statement is
    # replicated like any other DDL.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("updated_at", TSTZ, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("run_at", TSTZ, nullable=False),
        sa.Column("lease_owner", sa.Text),
        sa.Column("lease_until", TSTZ),
        sa.Column("done_at", TSTZ),
    )
    op.create_table(
        "embeddings",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("entity_id", sa.Uuid, nullable=False),
        sa.Column("chunk", sa.Integer, nullable=False),
        sa.Column("vec", Vector(384), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("embeddings")
    op.drop_table("jobs")
    op.drop_table("tasks")
