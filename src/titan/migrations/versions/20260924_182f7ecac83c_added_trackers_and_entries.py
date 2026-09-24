"""added trackers and entries

Revision ID: 182f7ecac83c
Revises: 03ccea8c307e
Create Date: 2026-09-24 16:07:33.082958
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "182f7ecac83c"
down_revision: str | None = "03ccea8c307e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trackers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "habit",
                "health",
                "finance",
                "custom",
                name="tracker_kind",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("min_value", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("max_value", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("target_value", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column(
            "target_period",
            sa.Enum(
                "day",
                "week",
                "month",
                name="tracker_period",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column(
            "target_direction",
            sa.Enum(
                "at_least",
                "at_most",
                name="tracker_direction",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=True,
        ),
        sa.Column("schedule", sa.String(length=200), nullable=True),
        sa.Column("archived", sa.Boolean(), server_default="false", nullable=False),
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
        sa.CheckConstraint(
            "(target_value IS NULL) = (target_period IS NULL)"
            " AND (target_value IS NULL) = (target_direction IS NULL)",
            name=op.f("ck_trackers_target"),
        ),
        sa.CheckConstraint("min_value <= max_value", name=op.f("ck_trackers_bounds")),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_trackers_owner_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trackers")),
    )
    op.create_index("ix_trackers_owner_id", "trackers", ["owner_id"], unique=False)
    op.create_table(
        "tracker_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tracker_id", sa.Uuid(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("category", sa.String(length=32), nullable=True),
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
            ["tracker_id"], ["trackers.id"], name=op.f("fk_tracker_entries_tracker_id_trackers")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tracker_entries")),
    )
    op.create_index(
        "ix_tracker_entries_tracker_id_at", "tracker_entries", ["tracker_id", "at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_tracker_entries_tracker_id_at", table_name="tracker_entries")
    op.drop_table("tracker_entries")
    op.drop_index("ix_trackers_owner_id", table_name="trackers")
    op.drop_table("trackers")
