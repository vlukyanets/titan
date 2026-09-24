"""tasks.priority and HNSW index on embeddings.vec

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
    op.add_column("tasks", sa.Column("priority", sa.Integer, nullable=False, server_default="0"))
    op.create_index(
        "embeddings_vec_hnsw",
        "embeddings",
        ["vec"],
        postgresql_using="hnsw",
        postgresql_ops={"vec": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("embeddings_vec_hnsw", table_name="embeddings")
    op.drop_column("tasks", "priority")
