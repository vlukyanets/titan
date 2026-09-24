"""added budgets

Revision ID: 719e78b3d56d
Revises: e4ae69c00fd7
Create Date: 2026-09-24 20:50:07.435513
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "719e78b3d56d"
down_revision: str | None = "e4ae69c00fd7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "budgets",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("limit_usd", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("limit_usd >= 0", name=op.f("ck_budgets_limit")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_budgets_user_id_users")),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_budgets")),
    )


def downgrade() -> None:
    op.drop_table("budgets")
