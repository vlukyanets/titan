"""added the daily plan time to planning prefs

Revision ID: 63c947cf3a90
Revises: 7d40e666fd31
Create Date: 2026-10-02 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "63c947cf3a90"
down_revision: str | None = "7d40e666fd31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Expand only: nullable, so NULL keeps meaning "no daily plan" for users who
    # turn it off, and older nodes ignore the column.
    op.add_column(
        "planning_prefs",
        sa.Column("daily_plan_at", sa.Time(), server_default="07:00", nullable=True),
    )


def downgrade() -> None:
    op.drop_column("planning_prefs", "daily_plan_at")
