"""added policy rules, approvals and the audit log

Revision ID: b58ef2622d65
Revises: e21717f28411
Create Date: 2026-09-24 13:03:14.550516
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b58ef2622d65"
down_revision: str | None = "e21717f28411"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("thread_id", sa.Uuid(), nullable=True),
        sa.Column("tool", sa.String(length=100), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column(
            "action_class",
            sa.Enum(
                "read",
                "write-internal",
                "external",
                "destructive",
                name="approval_action_class",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "approved",
                "executed",
                "failed",
                "rejected",
                "expired",
                name="approval_status",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.String(length=1000), nullable=True),
        sa.Column("audit_entry_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_approvals_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )
    op.create_index("ix_approvals_user_id_id", "approvals", ["user_id", "id"], unique=False)
    op.create_table(
        "audit_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("tool", sa.String(length=100), nullable=False),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column(
            "action_class",
            sa.Enum(
                "read",
                "write-internal",
                "external",
                "destructive",
                name="audit_action_class",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "decision",
            sa.Enum(
                "auto",
                "auto-undo",
                "confirm",
                "deny",
                name="audit_decision",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("input", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=True),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("undoable", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_audit_entries_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_entries")),
    )
    op.create_index("ix_audit_entries_user_id_id", "audit_entries", ["user_id", "id"], unique=False)
    op.create_table(
        "policy_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("domain", sa.String(length=32), nullable=False),
        sa.Column(
            "action_class",
            sa.Enum(
                "read",
                "write-internal",
                "external",
                "destructive",
                name="action_class",
                native_enum=False,
                create_constraint=True,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "decision",
            sa.Enum(
                "auto",
                "auto-undo",
                "confirm",
                "deny",
                name="policy_decision",
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
            ["user_id"], ["users.id"], name=op.f("fk_policy_rules_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_policy_rules")),
        sa.UniqueConstraint(
            "user_id",
            "domain",
            "action_class",
            name=op.f("uq_policy_rules_user_id"),
            postgresql_nulls_not_distinct=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("policy_rules")
    op.drop_index("ix_audit_entries_user_id_id", table_name="audit_entries")
    op.drop_table("audit_entries")
    op.drop_index("ix_approvals_user_id_id", table_name="approvals")
    op.drop_table("approvals")
