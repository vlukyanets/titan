"""Head schema as SQLAlchemy metadata (for `alembic check` / autogenerate drift)."""
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

metadata = sa.MetaData()

tasks = sa.Table(
    "tasks", metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("owner", sa.Text, nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
    sa.Column("priority", sa.Integer, nullable=False, server_default=sa.text("0")),
)
jobs = sa.Table(
    "jobs", metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("lease_owner", sa.Text),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("done_at", sa.DateTime(timezone=True)),
)
embeddings = sa.Table(
    "embeddings", metadata,
    sa.Column("id", sa.Uuid, primary_key=True),
    sa.Column("entity_id", sa.Uuid, nullable=False),
    sa.Column("chunk", sa.Integer, nullable=False),
    sa.Column("vec", Vector(384), nullable=False),
    sa.Index("embeddings_vec_idx", "vec", postgresql_using="cspann"),
)
