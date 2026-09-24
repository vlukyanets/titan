"""Projects, who they are shared with, and tasks."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from titan.storage.base import Base, str_enum
from titan.storage.ids import uuid7


class ProjectStatus(enum.StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class TaskStatus(enum.StrEnum):
    TODO = "todo"
    DOING = "doing"
    DONE = "done"
    CANCELLED = "cancelled"


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (Index("ix_projects_owner_id", "owner_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[ProjectStatus] = mapped_column(
        str_enum(ProjectStatus, "project_status"), default=ProjectStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectMember(Base):
    """A user a project is shared with, besides its owner."""

    __tablename__ = "project_members"
    # Looked up from the member's side when listing what they can see.
    __table_args__ = (Index("ix_project_members_user_id", "user_id"),)

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_owner_id_id", "owner_id", "id"),
        Index("ix_tasks_project_id", "project_id"),
        CheckConstraint("priority BETWEEN 1 AND 4", name="priority"),
        CheckConstraint("estimate_minutes > 0", name="estimate_minutes"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"))
    title: Mapped[str] = mapped_column(String(200))
    notes: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[TaskStatus] = mapped_column(
        str_enum(TaskStatus, "task_status"), default=TaskStatus.TODO
    )
    priority: Mapped[int] = mapped_column(SmallInteger, default=4, server_default="4")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimate_minutes: Mapped[int | None] = mapped_column(Integer)
    # RFC 5545 RRULE without DTSTART; due_at is the start.
    recurrence: Mapped[str | None] = mapped_column(String(200))
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
