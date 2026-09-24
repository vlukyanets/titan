"""added push retries

Revision ID: 03ccea8c307e
Revises: 20a08088766f
Create Date: 2026-09-24 15:13:21.483117
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "03ccea8c307e"
down_revision: str | None = "20a08088766f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("push_retries", sa.SmallInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "notifications", sa.Column("next_push_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_notifications_next_push_at", "notifications", ["next_push_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_next_push_at", table_name="notifications")
    op.drop_column("notifications", "next_push_at")
    op.drop_column("notifications", "push_retries")
