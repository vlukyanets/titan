"""added owner alerts to budgets

Revision ID: 7d40e666fd31
Revises: 719e78b3d56d
Create Date: 2026-09-24 22:46:54.804591
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d40e666fd31"
down_revision: str | None = "719e78b3d56d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "budgets",
        sa.Column(
            "owner_alerts",
            sa.Enum(
                "off",
                "exceeded",
                "all",
                name="owner_alerts",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            server_default="exceeded",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("budgets", "owner_alerts")
