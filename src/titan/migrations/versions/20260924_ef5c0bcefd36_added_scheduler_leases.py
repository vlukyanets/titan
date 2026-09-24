"""added scheduler leases

Revision ID: ef5c0bcefd36
Revises: 62c304122bc3
Create Date: 2026-09-24 14:45:57.739857
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ef5c0bcefd36"
down_revision: str | None = "62c304122bc3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduler_leases",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("holder", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("preferred_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_scheduler_leases")),
    )


def downgrade() -> None:
    op.drop_table("scheduler_leases")
