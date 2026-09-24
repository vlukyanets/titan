"""Projects and tasks (docs/spec/domains/tasks.md)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from titan.api.deps import CurrentPrincipal, Session
from titan.api.problems import PROBLEM_JSON
from titan.domains.tasks.models import Project, ProjectStatus, Task, TaskStatus
from titan.domains.tasks.service import (
    DEFAULT_PAGE,
    DESCRIPTION_LENGTH,
    MAX_ESTIMATE_MINUTES,
    MAX_PAGE,
    MAX_SHARED,
    MAX_TAGS,
    NOTES_LENGTH,
    TITLE_LENGTH,
    Completion,
    SharedProject,
    TaskQuery,
    TasksService,
)

projects_router = APIRouter(prefix="/projects", tags=["tasks"])
tasks_router = APIRouter(prefix="/tasks", tags=["tasks"])

_PROBLEM: dict[str, Any] = {"content": {PROBLEM_JSON: {}}}
_RRULE = "RFC 5545 RRULE without DTSTART; FREQ is DAILY, WEEKLY, MONTHLY or YEARLY, no COUNT"


def get_tasks(session: Session) -> TasksService:
    return TasksService(session)


Tasks = Annotated[TasksService, Depends(get_tasks)]


# ------------------------------------------------------------- projects


class ProjectOut(BaseModel):
    id: uuid.UUID
    owner_id: uuid.UUID
    title: str
    description: str
    status: ProjectStatus
    shared_with: list[uuid.UUID] = Field(description="Users besides the owner who see it")
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, shared: SharedProject) -> ProjectOut:
        p: Project = shared.project
        return cls(
            id=p.id,
            owner_id=p.owner_id,
            title=p.title,
            description=p.description,
            status=p.status,
            shared_with=shared.shared_with,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=TITLE_LENGTH)
    description: str = Field(default="", max_length=DESCRIPTION_LENGTH)
    shared_with: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_SHARED)


class ProjectPatch(BaseModel):
    """Only the fields sent change."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_LENGTH)
    description: str | None = Field(default=None, max_length=DESCRIPTION_LENGTH)
    status: ProjectStatus | None = None
    shared_with: list[uuid.UUID] | None = Field(
        default=None, max_length=MAX_SHARED, description="Replaces the whole list"
    )


@projects_router.get(
    "", summary="Projects the caller owns or that are shared with them", responses={401: _PROBLEM}
)
async def list_projects(
    principal: CurrentPrincipal,
    tasks: Tasks,
    project_status: Annotated[ProjectStatus | None, Query(alias="status")] = None,
) -> list[ProjectOut]:
    return [
        ProjectOut.of(p) for p in await tasks.projects(principal.user_id, status=project_status)
    ]


@projects_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def create_project(
    principal: CurrentPrincipal, tasks: Tasks, body: ProjectCreate
) -> ProjectOut:
    shared = await tasks.create_project(
        principal.user_id, body.title, description=body.description, shared_with=body.shared_with
    )
    return ProjectOut.of(shared)


@projects_router.get(
    "/{project_id}", summary="One project", responses={401: _PROBLEM, 404: _PROBLEM}
)
async def get_project(
    principal: CurrentPrincipal, tasks: Tasks, project_id: uuid.UUID
) -> ProjectOut:
    return ProjectOut.of(await tasks.get_project(principal.user_id, project_id))


@projects_router.patch(
    "/{project_id}",
    summary="Change a project (owner only)",
    responses={401: _PROBLEM, 403: _PROBLEM, 404: _PROBLEM, 422: _PROBLEM},
)
async def update_project(
    principal: CurrentPrincipal, tasks: Tasks, project_id: uuid.UUID, body: ProjectPatch
) -> ProjectOut:
    changes = body.model_dump(exclude_unset=True)
    return ProjectOut.of(await tasks.update_project(principal.user_id, project_id, changes))


@projects_router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a project (owner only); its tasks move to their owners' inboxes",
    responses={401: _PROBLEM, 403: _PROBLEM, 404: _PROBLEM},
)
async def delete_project(
    principal: CurrentPrincipal, tasks: Tasks, project_id: uuid.UUID
) -> Response:
    await tasks.delete_project(principal.user_id, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- tasks


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    owner_id: uuid.UUID
    project_id: uuid.UUID | None = Field(description="Empty for a task in its owner's inbox")
    title: str
    notes: str
    status: TaskStatus
    priority: int = Field(description="1 is the most urgent, 4 the least")
    due_at: datetime | None
    estimate_minutes: int | None
    recurrence: str | None = Field(description=_RRULE)
    tags: list[str]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def of(cls, task: Task) -> TaskOut:
        return cls.model_validate(task)


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=TITLE_LENGTH)
    notes: str = Field(default="", max_length=NOTES_LENGTH)
    priority: int = Field(default=4, ge=1, le=4)
    due_at: AwareDatetime | None = None
    estimate_minutes: int | None = Field(default=None, ge=1, le=MAX_ESTIMATE_MINUTES)
    recurrence: str | None = Field(
        default=None, max_length=200, description=_RRULE, examples=["FREQ=WEEKLY;BYDAY=MO"]
    )
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    project_id: uuid.UUID | None = None


class TaskPatch(BaseModel):
    """Only the fields sent change; `null` clears an optional field."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=TITLE_LENGTH)
    notes: str | None = Field(default=None, max_length=NOTES_LENGTH)
    status: TaskStatus | None = Field(
        default=None, description="todo, doing or cancelled; complete with POST …/complete"
    )
    priority: int | None = Field(default=None, ge=1, le=4)
    due_at: AwareDatetime | None = None
    estimate_minutes: int | None = Field(default=None, ge=1, le=MAX_ESTIMATE_MINUTES)
    recurrence: str | None = Field(default=None, max_length=200, description=_RRULE)
    tags: list[str] | None = Field(default=None, max_length=MAX_TAGS)
    project_id: uuid.UUID | None = Field(default=None, description="`null` moves it to the inbox")


class CompletionOut(BaseModel):
    task: TaskOut
    next: TaskOut | None = Field(description="The next occurrence of a recurring task")

    @classmethod
    def of(cls, completion: Completion) -> CompletionOut:
        following = TaskOut.of(completion.next) if completion.next is not None else None
        return cls(task=TaskOut.of(completion.task), next=following)


@tasks_router.get(
    "",
    summary="Tasks the caller can see, newest first",
    description="Page with `before`: pass the id of the last task you have.",
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def list_tasks(
    principal: CurrentPrincipal,
    tasks: Tasks,
    task_status: Annotated[
        list[TaskStatus] | None, Query(alias="status", description="Any of these; repeatable")
    ] = None,
    project_id: uuid.UUID | None = None,
    inbox: Annotated[bool, Query(description="Only the caller's tasks without a project")] = False,
    tag: Annotated[str | None, Query(max_length=32)] = None,
    due_before: AwareDatetime | None = None,
    q: Annotated[str | None, Query(max_length=200, description="Text in title or notes")] = None,
    before: Annotated[uuid.UUID | None, Query(description="Only older than this task")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> list[TaskOut]:
    query = TaskQuery(
        statuses=task_status or (),
        project_id=project_id,
        inbox=inbox,
        tag=tag,
        due_before=due_before,
        text=q,
    )
    items = await tasks.tasks(principal.user_id, query, before=before, limit=limit)
    return [TaskOut.of(t) for t in items]


@tasks_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a task",
    responses={401: _PROBLEM, 404: _PROBLEM, 422: _PROBLEM},
)
async def create_task(principal: CurrentPrincipal, tasks: Tasks, body: TaskCreate) -> TaskOut:
    task = await tasks.create_task(principal.user_id, **body.model_dump())
    return TaskOut.of(task)


@tasks_router.get("/{task_id}", summary="One task", responses={401: _PROBLEM, 404: _PROBLEM})
async def get_task(principal: CurrentPrincipal, tasks: Tasks, task_id: uuid.UUID) -> TaskOut:
    return TaskOut.of(await tasks.get_task(principal.user_id, task_id))


@tasks_router.patch(
    "/{task_id}",
    summary="Change a task",
    responses={401: _PROBLEM, 404: _PROBLEM, 422: _PROBLEM},
)
async def update_task(
    principal: CurrentPrincipal, tasks: Tasks, task_id: uuid.UUID, body: TaskPatch
) -> TaskOut:
    changes = body.model_dump(exclude_unset=True)
    return TaskOut.of(await tasks.update_task(principal.user_id, task_id, changes))


@tasks_router.post(
    "/{task_id}/complete",
    summary="Complete a task; a recurring one gets its next occurrence",
    responses={401: _PROBLEM, 404: _PROBLEM, 409: _PROBLEM},
)
async def complete_task(
    principal: CurrentPrincipal, tasks: Tasks, task_id: uuid.UUID
) -> CompletionOut:
    return CompletionOut.of(await tasks.complete_task(principal.user_id, task_id))


@tasks_router.delete(
    "/{task_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a task (its owner or its project's owner)",
    responses={401: _PROBLEM, 403: _PROBLEM, 404: _PROBLEM},
)
async def delete_task(principal: CurrentPrincipal, tasks: Tasks, task_id: uuid.UUID) -> Response:
    await tasks.delete_task(principal.user_id, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
