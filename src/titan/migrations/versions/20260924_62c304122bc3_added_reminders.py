"""added reminders

Revision ID: 62c304122bc3
Revises: 1042d125d88a
Create Date: 2026-09-24 14:01:30.531774
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "62c304122bc3"
down_revision: str | None = "1042d125d88a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.String(length=500), nullable=False),
        sa.Column("fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurs_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recurrence", sa.String(length=200), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "scheduled",
                "fired",
                "snoozed",
                "dismissed",
                name="reminder_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "link_type",
            sa.Enum(
                "task",
                "event",
                "tracker",
                name="reminder_link_type",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column("link_id", sa.Uuid(), nullable=True),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notification_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_reminders_owner_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminders")),
    )
    op.create_index("ix_reminders_owner_id_id", "reminders", ["owner_id", "id"], unique=False)
    op.create_index("ix_reminders_status_fire_at", "reminders", ["status", "fire_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_reminders_status_fire_at", table_name="reminders")
    op.drop_index("ix_reminders_owner_id_id", table_name="reminders")
    op.drop_table("reminders")
