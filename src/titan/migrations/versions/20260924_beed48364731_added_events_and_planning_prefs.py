"""added events and planning prefs

Revision ID: beed48364731
Revises: ef5c0bcefd36
Create Date: 2026-09-24 15:00:33.737319
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "beed48364731"
down_revision: str | None = "ef5c0bcefd36"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "event",
                "time_block",
                name="event_kind",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("location", sa.String(length=200), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("all_day", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("time_zone", sa.String(length=64), nullable=False),
        sa.Column("recurrence", sa.String(length=200), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
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
        sa.CheckConstraint("ends_at > starts_at", name=op.f("ck_events_ends_after_start")),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], name=op.f("fk_events_owner_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
    )
    op.create_index(
        "ix_events_owner_id_starts_at", "events", ["owner_id", "starts_at"], unique=False
    )
    op.create_table(
        "planning_prefs",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("time_zone", sa.String(length=64), server_default="UTC", nullable=False),
        sa.Column("work_start", sa.Time(), server_default="09:00", nullable=False),
        sa.Column("work_end", sa.Time(), server_default="17:00", nullable=False),
        sa.Column(
            "work_days", sa.ARRAY(sa.SmallInteger()), server_default="{1,2,3,4,5}", nullable=False
        ),
        sa.Column("buffer_minutes", sa.SmallInteger(), server_default="10", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "buffer_minutes BETWEEN 0 AND 240", name=op.f("ck_planning_prefs_buffer_minutes")
        ),
        sa.CheckConstraint("work_end > work_start", name=op.f("ck_planning_prefs_work_hours")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_planning_prefs_user_id_users")
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_planning_prefs")),
    )
    op.create_table(
        "event_attendees",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"], ["events.id"], name=op.f("fk_event_attendees_event_id_events")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_event_attendees_user_id_users")
        ),
        sa.PrimaryKeyConstraint("event_id", "user_id", name=op.f("pk_event_attendees")),
    )
    op.create_index("ix_event_attendees_user_id", "event_attendees", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_event_attendees_user_id", table_name="event_attendees")
    op.drop_table("event_attendees")
    op.drop_table("planning_prefs")
    op.drop_index("ix_events_owner_id_starts_at", table_name="events")
    op.drop_table("events")
