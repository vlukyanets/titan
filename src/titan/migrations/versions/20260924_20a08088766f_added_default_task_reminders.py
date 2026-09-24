"""added default task reminders

Revision ID: 20a08088766f
Revises: beed48364731
Create Date: 2026-09-24 15:09:15.685842
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20a08088766f"
down_revision: str | None = "beed48364731"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "planning_prefs",
        sa.Column(
            "default_reminder_minutes", sa.SmallInteger(), server_default="15", nullable=True
        ),
    )
    op.add_column(
        "reminders", sa.Column("is_default", sa.Boolean(), server_default="false", nullable=False)
    )


def downgrade() -> None:
    op.drop_column("reminders", "is_default")
    op.drop_column("planning_prefs", "default_reminder_minutes")
