"""initial schema

Adaptations for SQLite + cr-sqlite:
- uuid -> TEXT (canonical string), timestamptz -> TEXT (ISO-8601 UTC).
- vector(384) -> BLOB of 384 little-endian float32 (1536 bytes).
- cr-sqlite refuses NOT NULL columns without a DEFAULT (crsql_as_crr error), so
  every NOT NULL column gets a DEFAULT; primary keys must be NOT NULL.
- Every table is turned into a CRR with crsql_as_crr(). alembic_version is
  left as a normal (local, non-replicated) table.
- job_votes is an extra table used by the quorum-vote lease in S3.

Revision ID: 0001_initial
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

CRRS = ["tasks", "jobs", "embeddings", "job_votes"]


def upgrade() -> None:
    T = sa.Text
    op.create_table(
        "tasks",
        sa.Column("id", T, primary_key=True, nullable=False),
        sa.Column("owner", T, nullable=False, server_default=""),
        sa.Column("title", T, nullable=False, server_default=""),
        sa.Column("status", T, nullable=False, server_default=""),
        sa.Column("updated_at", T, nullable=False, server_default="1970-01-01T00:00:00Z"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", T, primary_key=True, nullable=False),
        sa.Column("kind", T, nullable=False, server_default=""),
        sa.Column("run_at", T, nullable=False, server_default="1970-01-01T00:00:00Z"),
        sa.Column("lease_owner", T, nullable=True),
        sa.Column("lease_until", T, nullable=True),
        sa.Column("done_at", T, nullable=True),
    )
    op.create_table(
        "embeddings",
        sa.Column("id", T, primary_key=True, nullable=False),
        sa.Column("entity_id", T, nullable=False, server_default=""),
        sa.Column("chunk", sa.Integer, nullable=False, server_default="0"),
        sa.Column("vec", sa.LargeBinary, nullable=False, server_default=sa.text("x''")),
    )
    op.create_table(
        "job_votes",
        sa.Column("job_id", T, primary_key=True, nullable=False),
        sa.Column("voter", T, primary_key=True, nullable=False),
        sa.Column("candidate", T, nullable=False, server_default=""),
        sa.Column("voted_at", T, nullable=True),
    )
    for t in CRRS:
        op.execute(f"SELECT crsql_as_crr('{t}')")


def downgrade() -> None:
    for t in reversed(CRRS):
        op.execute(f"SELECT crsql_as_table('{t}')")
        op.drop_table(t)
