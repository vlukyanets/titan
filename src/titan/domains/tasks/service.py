"""Projects and tasks, with the sharing rules of docs/spec/domains/tasks.md.

Every method takes the acting user's id and checks access itself. Items the
actor cannot see are reported as missing, so ids cannot be probed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.accounts.models import User
from titan.domains.calendar.zones import user_zone
from titan.domains.tasks import recurrence
from titan.domains.tasks.errors import (
    AlreadyDoneError,
    ForbiddenError,
    InvalidTaskError,
    NotFoundError,
)
from titan.domains.tasks.models import Project, ProjectMember, ProjectStatus, Task, TaskStatus

DEFAULT_PAGE = 50
MAX_PAGE = 100
TITLE_LENGTH = 200
DESCRIPTION_LENGTH = 4000
NOTES_LENGTH = 10000
TAG_LENGTH = 32
MAX_TAGS = 20
MAX_SHARED = 50
MAX_ESTIMATE_MINUTES = 7 * 24 * 60
PRIORITIES = range(1, 5)

TASK_FIELDS = frozenset(
    {
        "title",
        "notes",
        "status",
        "priority",
        "due_at",
        "estimate_minutes",
        "recurrence",
        "tags",
        "project_id",
    }
)
PROJECT_FIELDS = frozenset({"title", "description", "status", "shared_with"})


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid(value: object, name: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise InvalidTaskError(f"{name} is not an id") from None


def _title(value: object) -> str:
    title = "" if value is None else " ".join(str(value).split())
    if not title:
        raise InvalidTaskError("the title is empty")
    if len(title) > TITLE_LENGTH:
        raise InvalidTaskError(f"the title is longer than {TITLE_LENGTH} characters")
    return title


def _text(value: object, limit: int, name: str) -> str:
    text = str(value).strip()
    if len(text) > limit:
        raise InvalidTaskError(f"the {name} is longer than {limit} characters")
    return text


def _tags(values: Iterable[object]) -> list[str]:
    tags: list[str] = []
    for value in values:
        tag = str(value).strip().lower()
        if not tag or len(tag) > TAG_LENGTH:
            raise InvalidTaskError(f"a tag must be 1 to {TAG_LENGTH} characters")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise InvalidTaskError(f"a task has at most {MAX_TAGS} tags")
    return tags


def _priority(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value not in PRIORITIES:
        raise InvalidTaskError("the priority is 1 (most urgent) to 4")
    return value


def _estimate(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidTaskError("the estimate is a number of minutes")
    if not 0 < value <= MAX_ESTIMATE_MINUTES:
        raise InvalidTaskError(f"the estimate is 1 to {MAX_ESTIMATE_MINUTES} minutes")
    return value


def _due(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidTaskError("due_at needs a time zone")
    return value.astimezone(UTC)


def _recurrence(value: object) -> str | None:
    return None if value is None else recurrence.normalize(str(value))


@dataclass(frozen=True)
class SharedProject:
    project: Project
    # Members besides the owner, in the order they were stored.
    shared_with: list[uuid.UUID]


@dataclass(frozen=True)
class TaskQuery:
    statuses: Sequence[TaskStatus] = ()
    project_id: uuid.UUID | None = None
    # Only the actor's own tasks without a project.
    inbox: bool = False
    tag: str | None = None
    due_before: datetime | None = None
    # Text in the title or notes, case-insensitive.
    text: str | None = None


@dataclass(frozen=True)
class Completion:
    task: Task
    next: Task | None = None


@dataclass
class _Draft:
    title: str
    notes: str = ""
    priority: int = 4
    due_at: datetime | None = None
    estimate_minutes: int | None = None
    recurrence: str | None = None
    tags: list[str] = field(default_factory=list)
    project_id: uuid.UUID | None = None


class TasksService:
    def __init__(self, session: AsyncSession, *, commit: bool = True) -> None:
        self.session = session
        # Agent tools pass commit=False: their caller commits the change together
        # with its audit entry.
        self._commit = commit

    async def _done(self) -> None:
        if self._commit:
            await self.session.commit()
        else:
            await self.session.flush()

    # ------------------------------------------------------------- access

    @staticmethod
    def _member_of(actor: uuid.UUID) -> ColumnElement[bool]:
        return exists().where(
            ProjectMember.project_id == Project.id, ProjectMember.user_id == actor
        )

    def _visible_projects(self, actor: uuid.UUID) -> ColumnElement[bool]:
        return or_(Project.owner_id == actor, self._member_of(actor))

    def _visible_tasks(self, actor: uuid.UUID) -> ColumnElement[bool]:
        shared = select(Project.id).where(self._visible_projects(actor)).scalar_subquery()
        return or_(Task.owner_id == actor, Task.project_id.in_(shared))

    async def _members(self, project_id: uuid.UUID) -> list[uuid.UUID]:
        rows = await self.session.scalars(
            select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)
        )
        return list(rows.all())

    async def _project(
        self, actor: uuid.UUID, project_id: uuid.UUID, *, lock: bool = False
    ) -> Project:
        query = select(Project).where(Project.id == project_id, self._visible_projects(actor))
        if lock:
            query = query.with_for_update()
        project = await self.session.scalar(query)
        if project is None:
            raise NotFoundError("project not found")
        return project

    async def _shared_with(self, owner: uuid.UUID, ids: Iterable[object]) -> list[uuid.UUID]:
        wanted: list[uuid.UUID] = []
        for value in ids:
            user_id = _uuid(value, "shared_with")
            if user_id != owner and user_id not in wanted:
                wanted.append(user_id)
        if len(wanted) > MAX_SHARED:
            raise InvalidTaskError(f"a project is shared with at most {MAX_SHARED} users")
        if wanted:
            found = set(
                (
                    await self.session.scalars(
                        select(User.id).where(User.id.in_(wanted), User.disabled_at.is_(None))
                    )
                ).all()
            )
            if len(found) != len(wanted):
                raise InvalidTaskError("shared_with names a user that does not exist")
        return wanted

    # ----------------------------------------------------------- projects

    async def create_project(
        self,
        actor: uuid.UUID,
        title: str,
        *,
        description: str = "",
        shared_with: Iterable[uuid.UUID] = (),
    ) -> SharedProject:
        project = Project(
            owner_id=actor,
            title=_title(title),
            description=_text(description, DESCRIPTION_LENGTH, "description"),
        )
        members = await self._shared_with(actor, shared_with)
        self.session.add(project)
        await self.session.flush()
        self.session.add_all(ProjectMember(project_id=project.id, user_id=m) for m in members)
        await self._done()
        return SharedProject(project, members)

    async def projects(
        self, actor: uuid.UUID, *, status: ProjectStatus | None = None
    ) -> list[SharedProject]:
        query = select(Project).where(self._visible_projects(actor))
        if status is not None:
            query = query.where(Project.status == status)
        projects = (await self.session.scalars(query.order_by(Project.id))).all()
        members: dict[uuid.UUID, list[uuid.UUID]] = {p.id: [] for p in projects}
        if members:
            rows = await self.session.execute(
                select(ProjectMember.project_id, ProjectMember.user_id).where(
                    ProjectMember.project_id.in_(members)
                )
            )
            for project_id, user_id in rows:
                members[project_id].append(user_id)
        return [SharedProject(p, members[p.id]) for p in projects]

    async def get_project(self, actor: uuid.UUID, project_id: uuid.UUID) -> SharedProject:
        project = await self._project(actor, project_id)
        return SharedProject(project, await self._members(project.id))

    async def update_project(
        self, actor: uuid.UUID, project_id: uuid.UUID, changes: Mapping[str, Any]
    ) -> SharedProject:
        unknown = set(changes) - PROJECT_FIELDS
        if unknown:
            raise InvalidTaskError(f"a project has no field {sorted(unknown)[0]}")
        project = await self._project(actor, project_id, lock=True)
        if project.owner_id != actor:
            raise ForbiddenError("only the project's owner can change it")
        values: dict[str, Any] = {}
        if "title" in changes:
            values["title"] = _title(changes["title"])
        if "description" in changes:
            values["description"] = _text(
                changes["description"] or "", DESCRIPTION_LENGTH, "description"
            )
        if "status" in changes:
            try:
                values["status"] = ProjectStatus(changes["status"])
            except ValueError:
                raise InvalidTaskError("the status is active or archived") from None
        members = await self._members(project.id)
        if "shared_with" in changes:
            members = await self._shared_with(actor, changes["shared_with"] or ())
            await self.session.execute(
                delete(ProjectMember).where(ProjectMember.project_id == project.id)
            )
            self.session.add_all(ProjectMember(project_id=project.id, user_id=m) for m in members)
        for name, value in values.items():
            setattr(project, name, value)
        project.updated_at = _now()
        await self._done()
        return SharedProject(project, members)

    async def delete_project(self, actor: uuid.UUID, project_id: uuid.UUID) -> None:
        project = await self._project(actor, project_id, lock=True)
        if project.owner_id != actor:
            raise ForbiddenError("only the project's owner can delete it")
        # Tasks go back to their owners' inboxes, so no member loses their work.
        await self.session.execute(
            update(Task)
            .where(Task.project_id == project.id)
            .values(project_id=None, updated_at=_now())
        )
        await self.session.execute(
            delete(ProjectMember).where(ProjectMember.project_id == project.id)
        )
        await self.session.delete(project)
        await self._done()

    # -------------------------------------------------------------- tasks

    async def _target_project(self, actor: uuid.UUID, project_id: object) -> uuid.UUID | None:
        if project_id is None:
            return None
        project = await self._project(actor, _uuid(project_id, "project_id"))
        if project.status is ProjectStatus.ARCHIVED:
            raise InvalidTaskError("the project is archived")
        return project.id

    async def create_task(
        self,
        actor: uuid.UUID,
        title: str,
        *,
        notes: str = "",
        priority: int = 4,
        due_at: datetime | None = None,
        estimate_minutes: int | None = None,
        recurrence: str | None = None,
        tags: Iterable[str] = (),
        project_id: uuid.UUID | None = None,
    ) -> Task:
        draft = _Draft(
            title=_title(title),
            notes=_text(notes, NOTES_LENGTH, "notes"),
            priority=_priority(priority),
            due_at=_due(due_at),
            estimate_minutes=_estimate(estimate_minutes),
            recurrence=_recurrence(recurrence),
            tags=_tags(tags),
            project_id=await self._target_project(actor, project_id),
        )
        if draft.recurrence and draft.due_at is None:
            raise InvalidTaskError("a recurring task needs due_at")
        task = Task(owner_id=actor, **draft.__dict__)
        self.session.add(task)
        await self._done()
        return task

    async def tasks(
        self,
        actor: uuid.UUID,
        query: TaskQuery | None = None,
        *,
        before: uuid.UUID | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> list[Task]:
        query = query or TaskQuery()
        stmt = select(Task).where(self._visible_tasks(actor))
        if query.statuses:
            stmt = stmt.where(Task.status.in_(query.statuses))
        if query.project_id is not None:
            stmt = stmt.where(Task.project_id == query.project_id)
        if query.inbox:
            stmt = stmt.where(Task.owner_id == actor, Task.project_id.is_(None))
        if query.tag:
            stmt = stmt.where(Task.tags.contains([query.tag.strip().lower()]))
        if query.due_before is not None:
            stmt = stmt.where(Task.due_at < query.due_before)
        if query.text:
            pattern = "%" + _escape_like(query.text.strip()) + "%"
            stmt = stmt.where(
                or_(Task.title.ilike(pattern, escape="\\"), Task.notes.ilike(pattern, escape="\\"))
            )
        if before is not None:
            stmt = stmt.where(Task.id < before)
        stmt = stmt.order_by(Task.id.desc()).limit(max(1, min(limit, MAX_PAGE)))
        return list((await self.session.scalars(stmt)).all())

    async def get_task(self, actor: uuid.UUID, task_id: uuid.UUID, *, lock: bool = False) -> Task:
        stmt = select(Task).where(Task.id == task_id, self._visible_tasks(actor))
        if lock:
            stmt = stmt.with_for_update(of=Task)
        task = await self.session.scalar(stmt)
        if task is None:
            raise NotFoundError("task not found")
        return task

    async def update_task(
        self, actor: uuid.UUID, task_id: uuid.UUID, changes: Mapping[str, Any]
    ) -> Task:
        unknown = set(changes) - TASK_FIELDS
        if unknown:
            raise InvalidTaskError(f"a task has no field {sorted(unknown)[0]}")
        task = await self.get_task(actor, task_id, lock=True)
        # Everything is checked before the task changes, so a refused update
        # leaves it untouched.
        values: dict[str, Any] = {}
        if "title" in changes:
            values["title"] = _title(changes["title"])
        if "notes" in changes:
            values["notes"] = _text(changes["notes"] or "", NOTES_LENGTH, "notes")
        if "priority" in changes:
            values["priority"] = _priority(changes["priority"])
        if "due_at" in changes:
            values["due_at"] = _due(changes["due_at"])
        if "estimate_minutes" in changes:
            values["estimate_minutes"] = _estimate(changes["estimate_minutes"])
        if "recurrence" in changes:
            values["recurrence"] = _recurrence(changes["recurrence"])
        if "tags" in changes:
            values["tags"] = _tags(changes["tags"] or ())
        if "project_id" in changes and changes["project_id"] != task.project_id:
            values["project_id"] = await self._target_project(actor, changes["project_id"])
        if "status" in changes:
            values.update(self._status_change(task, changes["status"]))
        if values.get("recurrence", task.recurrence) and values.get("due_at", task.due_at) is None:
            raise InvalidTaskError("a recurring task needs due_at")
        for name, value in values.items():
            setattr(task, name, value)
        task.updated_at = _now()
        await self._done()
        return task

    @staticmethod
    def _status_change(task: Task, value: object) -> dict[str, Any]:
        try:
            status = TaskStatus(str(value))
        except ValueError:
            raise InvalidTaskError("the status is todo, doing, done or cancelled") from None
        if status is TaskStatus.DONE:
            if task.status is not TaskStatus.DONE:
                raise InvalidTaskError("complete a task with POST /tasks/{id}/complete")
            return {}
        return {"status": status, "completed_at": None}

    async def complete_task(
        self, actor: uuid.UUID, task_id: uuid.UUID, *, now: datetime | None = None
    ) -> Completion:
        task = await self.get_task(actor, task_id, lock=True)
        if task.status is TaskStatus.DONE:
            raise AlreadyDoneError("the task is already done")
        now = now or _now()
        task.status = TaskStatus.DONE
        task.completed_at = now
        task.updated_at = now
        following = None
        if task.recurrence and task.due_at is not None:
            tz = await user_zone(self.session, task.owner_id)
            due = recurrence.next_occurrence(task.recurrence, task.due_at, now, tz)
            if due is not None:
                following = Task(
                    owner_id=task.owner_id,
                    project_id=task.project_id,
                    title=task.title,
                    notes=task.notes,
                    priority=task.priority,
                    due_at=due,
                    estimate_minutes=task.estimate_minutes,
                    recurrence=task.recurrence,
                    tags=list(task.tags),
                )
                self.session.add(following)
                # The rule moves on with the series, so reopening and completing
                # this occurrence again cannot create a second copy.
                task.recurrence = None
        await self._done()
        return Completion(task, following)

    async def delete_task(self, actor: uuid.UUID, task_id: uuid.UUID) -> None:
        task = await self.get_task(actor, task_id, lock=True)
        if task.owner_id != actor:
            project_owner = None
            if task.project_id is not None:
                project_owner = await self.session.scalar(
                    select(Project.owner_id).where(Project.id == task.project_id)
                )
            if project_owner != actor:
                raise ForbiddenError("only the task's owner or its project's owner can delete it")
        await self.session.delete(task)
        await self._done()


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
