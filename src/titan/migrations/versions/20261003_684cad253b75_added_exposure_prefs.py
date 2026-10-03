"""added exposure prefs

Revision ID: 684cad253b75
Revises: 28bf86f1609b
Create Date: 2026-10-03 10:26:47.553607
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "684cad253b75"
down_revision: str | None = "28bf86f1609b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exposure_prefs",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "chat_health",
            sa.Enum(
                "full",
                "aggregates",
                name="chat_health",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "chat_finance",
            sa.Enum(
                "full",
                "aggregates",
                name="chat_finance",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "workflows_health",
            sa.Enum(
                "full",
                "aggregates",
                name="workflows_health",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "workflows_finance",
            sa.Enum(
                "full",
                "aggregates",
                name="workflows_finance",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_exposure_prefs_user_id_users")
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_exposure_prefs")),
    )


def downgrade() -> None:
    op.drop_table("exposure_prefs")
