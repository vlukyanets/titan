"""initial schema: tasks, jobs, embeddings

Revision ID: 0001_initial
Revises:
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


class Vector(UserDefinedType):
    """Minimal pgvector column type (YugabyteDB ships pgvector 0.8.0-yb-1.0)."""

    cache_ok = True

    def __init__(self, dim: int):
        self.dim = dim

    def get_col_spec(self, **kw):
        return f"vector({self.dim})"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    )
    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.Text),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("done_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "embeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk", sa.Integer, nullable=False),
        sa.Column("vec", Vector(384), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("embeddings")
    op.drop_table("jobs")
    op.drop_table("tasks")
