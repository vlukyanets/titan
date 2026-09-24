"""Task priority column and an ANN index on embeddings

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

from migrations.flavour import flavour

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

INDEX = {
    "postgres": "CREATE INDEX ix_embeddings_ann ON embeddings USING hnsw (embedding vector_l2_ops)",
    "yugabyte": "CREATE INDEX ix_embeddings_ann ON embeddings USING ybhnsw (embedding vector_l2_ops)",
    "cockroach": "CREATE VECTOR INDEX ix_embeddings_ann ON embeddings (embedding)",
}


def upgrade() -> None:
    op.add_column("tasks", sa.Column("priority", sa.Integer()))
    op.execute(INDEX[flavour()])


def downgrade() -> None:
    op.execute("DROP INDEX ix_embeddings_ann")
    op.drop_column("tasks", "priority")
