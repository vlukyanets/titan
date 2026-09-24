"""tasks.priority and ANN index on embeddings.vec

Revision ID: 0002_priority_and_ann_index
Revises: 0001_initial

CockroachDB specifics:
- Vector indexes are gated by the cluster setting feature.vector_index.enabled
  (default false). SET CLUSTER SETTING cannot run inside a multi-statement
  transaction, so it runs in an Alembic autocommit block.
- The index type is C-SPANN (hierarchical k-means partitions), not HNSW.
  `USING cspann` (and even `USING hnsw`) are accepted by CREATE INDEX and both
  produce a `VECTOR INDEX`.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_priority_and_ann_index"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("SET CLUSTER SETTING feature.vector_index.enabled = true")
    op.add_column(
        "tasks",
        sa.Column("priority", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    op.create_index(
        "embeddings_vec_idx", "embeddings", ["vec"],
        postgresql_using="cspann", postgresql_ops={"vec": "vector_l2_ops"},
    )


def downgrade() -> None:
    op.drop_index("embeddings_vec_idx", table_name="embeddings")
    op.drop_column("tasks", "priority")
