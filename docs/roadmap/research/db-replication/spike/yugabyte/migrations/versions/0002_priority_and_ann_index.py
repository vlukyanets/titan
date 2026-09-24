"""tasks.priority and ANN index on embeddings.vec

Revision ID: 0002_priority_and_ann_index
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_priority_and_ann_index"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("priority", sa.Integer, nullable=False, server_default=sa.text("0")),
    )
    # YugabyteDB's ANN access method is `ybhnsw` (DocDB Vector LSM); `USING hnsw`
    # is accepted and rewritten to it. Plain Alembic op.create_index is used; the
    # vectors are unit length, so cosine distance is the natural metric.
    op.create_index(
        "embeddings_vec_ybhnsw",
        "embeddings",
        ["vec"],
        postgresql_using="ybhnsw",
        postgresql_ops={"vec": "vector_cosine_ops"},
        postgresql_with={"m": 16, "ef_construction": 64},
    )


def downgrade() -> None:
    op.drop_index("embeddings_vec_ybhnsw", table_name="embeddings")
    op.drop_column("tasks", "priority")
