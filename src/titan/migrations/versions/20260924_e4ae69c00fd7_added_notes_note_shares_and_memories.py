"""added notes, note shares and memories

Revision ID: e4ae69c00fd7
Revises: 182f7ecac83c
Create Date: 2026-09-24 17:41:15.541901
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e4ae69c00fd7"
down_revision: str | None = "182f7ecac83c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("statement", sa.String(length=500), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "chat",
                "note",
                "user",
                name="memory_source",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("source_id", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
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
        sa.Column(
            "last_confirmed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(source = 'user') = (source_id IS NULL)", name=op.f("ck_memories_source_id")
        ),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name=op.f("ck_memories_confidence")),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name=op.f("fk_memories_owner_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memories")),
    )
    op.create_index("ix_memories_owner_id_id", "memories", ["owner_id", "id"], unique=False)
    op.create_table(
        "notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), server_default="", nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "tags", postgresql.ARRAY(sa.String(length=32)), server_default="{}", nullable=False
        ),
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
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], name=op.f("fk_notes_owner_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notes")),
    )
    op.create_index(
        "ix_notes_owner_id_updated_at", "notes", ["owner_id", "updated_at"], unique=False
    )
    op.create_table(
        "note_shares",
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["note_id"], ["notes.id"], name=op.f("fk_note_shares_note_id_notes")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_note_shares_user_id_users")
        ),
        sa.PrimaryKeyConstraint("note_id", "user_id", name=op.f("pk_note_shares")),
    )
    op.create_index("ix_note_shares_user_id", "note_shares", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_note_shares_user_id", table_name="note_shares")
    op.drop_table("note_shares")
    op.drop_index("ix_notes_owner_id_updated_at", table_name="notes")
    op.drop_table("notes")
    op.drop_index("ix_memories_owner_id_id", table_name="memories")
    op.drop_table("memories")
