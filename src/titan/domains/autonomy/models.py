"""Policy rules, approval requests and the audit log (docs/spec/domains/autonomy.md)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from titan.storage.base import Base, str_enum
from titan.storage.ids import uuid7


class ActionClass(enum.StrEnum):
    READ = "read"
    WRITE_INTERNAL = "write-internal"
    EXTERNAL = "external"
    DESTRUCTIVE = "destructive"


class Decision(enum.StrEnum):
    AUTO = "auto"
    AUTO_UNDO = "auto-undo"
    CONFIRM = "confirm"
    DENY = "deny"


class ApprovalStatus(enum.StrEnum):
    PENDING = "pending"
    # Claimed by an approval and running; a crash leaves it here, visible as such.
    APPROVED = "approved"
    EXECUTED = "executed"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"


# Domains a rule can name; the ones without tools yet get theirs in M2.
DOMAINS = (
    "accounts",
    "chat",
    "notifications",
    "tasks",
    "calendar",
    "notes",
    "memory",
    "trackers",
    "reminders",
)

DEFAULT_DECISIONS = {
    ActionClass.READ: Decision.AUTO,
    ActionClass.WRITE_INTERNAL: Decision.AUTO_UNDO,
    ActionClass.EXTERNAL: Decision.CONFIRM,
    ActionClass.DESTRUCTIVE: Decision.CONFIRM,
}


class PolicyRule(Base):
    __tablename__ = "policy_rules"
    # One rule per scope, domain and action class; a NULL user is the household
    # default, and NULLS NOT DISTINCT keeps that unique too.
    __table_args__ = (
        UniqueConstraint("user_id", "domain", "action_class", postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    domain: Mapped[str] = mapped_column(String(32))
    action_class: Mapped[ActionClass] = mapped_column(str_enum(ActionClass, "action_class"))
    decision: Mapped[Decision] = mapped_column(str_enum(Decision, "policy_decision"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Approval(Base):
    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_user_id_id", "user_id", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    # The thread to post the result to. No foreign key: deleting the thread
    # must not delete or block the request, which stays in the history.
    thread_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    tool: Mapped[str] = mapped_column(String(100))
    domain: Mapped[str] = mapped_column(String(32))
    action_class: Mapped[ActionClass] = mapped_column(
        str_enum(ActionClass, "approval_action_class")
    )
    # Exactly what runs on approval.
    input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    summary: Mapped[str] = mapped_column(String(500))
    status: Mapped[ApprovalStatus] = mapped_column(str_enum(ApprovalStatus, "approval_status"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(String(1000))
    audit_entry_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


class AuditEntry(Base):
    __tablename__ = "audit_entries"
    __table_args__ = (Index("ix_audit_entries_user_id_id", "user_id", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    tool: Mapped[str] = mapped_column(String(100))
    domain: Mapped[str] = mapped_column(String(32))
    action_class: Mapped[ActionClass] = mapped_column(str_enum(ActionClass, "audit_action_class"))
    decision: Mapped[Decision] = mapped_column(str_enum(Decision, "audit_decision"))
    summary: Mapped[str] = mapped_column(String(500))
    input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    entity_type: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    undoable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
