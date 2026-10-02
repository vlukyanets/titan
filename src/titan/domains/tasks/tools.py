"""Agent tools of the tasks domain (docs/spec/domains/tasks.md#agent-tools).

Every call goes through `TasksService` with `commit=False`, so the sharing rules
apply and the wrapper commits the change with its audit entry. A write that
touches a project shared with another user is `external`, so the default policy
asks before it runs. Times are read and shown in the user's time zone.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select

from titan.domains.accounts.models import User
from titan.domains.autonomy.errors import UndoConflictError
from titan.domains.autonomy.models import ActionClass
from titan.domains.autonomy.tools import (
    Change,
    RunFn,
    ToolContext,
    ToolResult,
    ToolSpec,
    guarded,
)
from titan.domains.calendar.zones import parse_local, show_local, user_zone
from titan.domains.reminders.defaults import drop_task_reminders, sync_task_reminder
from titan.domains.tasks.errors import InvalidTaskError, TasksError
from titan.domains.tasks.models import (
    Project,
    ProjectMember,
    ProjectStatus,
    Task,
    TaskStatus,
)
from titan.domains.tasks.service import (
    DESCRIPTION_LENGTH,
    MAX_PAGE,
    MAX_SHARED,
    MAX_TAGS,
    NOTES_LENGTH,
    TAG_LENGTH,
    TITLE_LENGTH,
    SharedProject,
    TaskQuery,
    TasksService,
)

DOMAIN = "tasks"
DEFAULT_LIST = 30

# ------------------------------------------------------------------ helpers


def _service(context: ToolContext) -> TasksService:
    return TasksService(context.session, commit=False)


def _id(args: Mapping[str, Any], key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(args[key]))
    except ValueError:
        raise InvalidTaskError(f"{key} is not an id") from None


def _when(value: object, zone: ZoneInfo) -> datetime | None:
    if value is None:
        return None
    try:
        return parse_local(str(value), zone)
    except ValueError:
        raise InvalidTaskError(f"{value} is not an ISO 8601 date and time") from None


async def _user_ids(context: ToolContext, usernames: Iterable[object]) -> list[uuid.UUID]:
    wanted = [str(u).strip().lower() for u in usernames]
    found = dict(
        (
            await context.session.execute(
                select(User.username, User.id).where(
                    User.username.in_(wanted), User.disabled_at.is_(None)
                )
            )
        )
        .tuples()
        .all()
    )
    missing = [u for u in wanted if u not in found]
    if missing:
        raise InvalidTaskError(f"there is no household member called {missing[0]}")
    return [found[u] for u in wanted]


async def _usernames(context: ToolContext, ids: Iterable[uuid.UUID]) -> list[str]:
    ids = list(ids)
    if not ids:
        return []
    names = dict(
        (await context.session.execute(select(User.id, User.username).where(User.id.in_(ids))))
        .tuples()
        .all()
    )
    return [names.get(i, "?") for i in ids]


def _guard(run: RunFn) -> RunFn:
    return guarded(run, (TasksError,))


# ------------------------------------------------------------ snapshots

_TASK_STATE = (
    "project_id",
    "title",
    "notes",
    "status",
    "priority",
    "due_at",
    "estimate_minutes",
    "recurrence",
    "tags",
    "completed_at",
)
_TIMES = ("due_at", "completed_at")


def task_state(task: Task) -> dict[str, Any]:
    """What undo restores, as JSON."""
    return {
        "project_id": str(task.project_id) if task.project_id else None,
        "title": task.title,
        "notes": task.notes,
        "status": task.status.value,
        "priority": task.priority,
        "due_at": task.due_at.astimezone(UTC).isoformat() if task.due_at else None,
        "estimate_minutes": task.estimate_minutes,
        "recurrence": task.recurrence,
        "tags": list(task.tags),
        "completed_at": (
            task.completed_at.astimezone(UTC).isoformat() if task.completed_at else None
        ),
    }


def _restore_task(task: Task, state: Mapping[str, Any]) -> None:
    task.project_id = uuid.UUID(state["project_id"]) if state["project_id"] else None
    task.title = state["title"]
    task.notes = state["notes"]
    task.status = TaskStatus(state["status"])
    task.priority = state["priority"]
    for name in _TIMES:
        setattr(task, name, datetime.fromisoformat(state[name]) if state[name] else None)
    task.estimate_minutes = state["estimate_minutes"]
    task.recurrence = state["recurrence"]
    task.tags = list(state["tags"])
    task.updated_at = datetime.now(UTC)


async def project_state(context: ToolContext, shared: SharedProject) -> dict[str, Any]:
    project = shared.project
    return {
        "title": project.title,
        "description": project.description,
        "status": project.status.value,
        "shared_with": sorted(await _usernames(context, shared.shared_with)),
    }


async def _undo_target_task(context: ToolContext, change: Change) -> Task:
    try:
        task = await _service(context).get_task(
            context.user_id, uuid.UUID(change.entity_id), lock=True
        )
    except TasksError:
        raise UndoConflictError("the task no longer exists") from None
    current = task_state(task)
    if {k: current[k] for k in _TASK_STATE} != {k: change.after[k] for k in _TASK_STATE}:
        raise UndoConflictError("the task was changed again since")
    return task


# --------------------------------------------------------- classification


def _shared(project: SharedProject, actor: uuid.UUID) -> bool:
    return bool(project.shared_with) or project.project.owner_id != actor


async def _project_shared(context: ToolContext, project_id: object) -> bool:
    if project_id is None:
        return False
    try:
        project = await _service(context).get_project(context.user_id, uuid.UUID(str(project_id)))
    except (TasksError, ValueError):
        return False
    return _shared(project, context.user_id)


async def _task_shared(context: ToolContext, task_id: object) -> bool:
    try:
        task = await _service(context).get_task(context.user_id, uuid.UUID(str(task_id)))
    except (TasksError, ValueError):
        return False
    return task.owner_id != context.user_id or await _project_shared(context, task.project_id)


def _external_if(shared: bool) -> ActionClass:
    return ActionClass.EXTERNAL if shared else ActionClass.WRITE_INTERNAL


async def _classify_create_task(context: ToolContext, args: Mapping[str, Any]) -> ActionClass:
    return _external_if(await _project_shared(context, args.get("project_id")))


async def _classify_task_write(context: ToolContext, args: Mapping[str, Any]) -> ActionClass:
    shared = await _task_shared(context, args.get("task_id"))
    if not shared and args.get("project_id") is not None:
        shared = await _project_shared(context, args["project_id"])
    return _external_if(shared)


async def _classify_project_write(context: ToolContext, args: Mapping[str, Any]) -> ActionClass:
    return _external_if(
        bool(args.get("shared_with")) or await _project_shared(context, args.get("project_id"))
    )


async def _task_title(context: ToolContext, args: Mapping[str, Any]) -> str | None:
    try:
        task = await _service(context).get_task(context.user_id, uuid.UUID(str(args["task_id"])))
    except (TasksError, ValueError, KeyError):
        return None
    return task.title


async def _project_title(context: ToolContext, args: Mapping[str, Any]) -> str | None:
    try:
        shared = await _service(context).get_project(
            context.user_id, uuid.UUID(str(args["project_id"]))
        )
    except (TasksError, ValueError, KeyError):
        return None
    return shared.project.title


# ------------------------------------------------------------- formatting


def _task_line(task: Task, zone: ZoneInfo, projects: Mapping[uuid.UUID, str]) -> str:
    parts = [f"- {task.id} [{task.status.value}] {task.title}"]
    if task.due_at:
        parts.append(f"due {show_local(task.due_at, zone)}")
    parts.append(f"P{task.priority}")
    if task.estimate_minutes:
        parts.append(f"{task.estimate_minutes} min")
    parts.append(f"project “{projects.get(task.project_id, '?')}”" if task.project_id else "inbox")
    if task.recurrence:
        parts.append(f"repeats {task.recurrence}")
    if task.tags:
        parts.append(" ".join(f"#{t}" for t in task.tags))
    return " · ".join(parts)


async def _project_titles(context: ToolContext) -> dict[uuid.UUID, str]:
    return {
        p.project.id: p.project.title for p in await _service(context).projects(context.user_id)
    }


# ------------------------------------------------------------------ tasks


async def _list_tasks(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    zone = await user_zone(context.session, context.user_id)
    statuses = [TaskStatus(s) for s in args.get("status") or ("todo", "doing")]
    query = TaskQuery(
        statuses=statuses,
        project_id=_id(args, "project_id") if args.get("project_id") else None,
        inbox=bool(args.get("inbox")),
        tag=args.get("tag"),
        due_before=_when(args.get("due_before"), zone),
        text=args.get("text"),
    )
    tasks = await _service(context).tasks(
        context.user_id, query, limit=int(args.get("limit", DEFAULT_LIST))
    )
    if not tasks:
        return ToolResult("No tasks match.")
    projects = await _project_titles(context)
    lines = [_task_line(t, zone, projects) for t in tasks]
    return ToolResult(f"{len(tasks)} tasks, newest first:\n" + "\n".join(lines))


async def _get_task(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    zone = await user_zone(context.session, context.user_id)
    task = await _service(context).get_task(context.user_id, _id(args, "task_id"))
    line = _task_line(task, zone, await _project_titles(context))
    details = [line]
    if task.completed_at:
        details.append(f"Completed {show_local(task.completed_at, zone)}")
    if task.owner_id != context.user_id:
        details.append(f"Owner: {(await _usernames(context, [task.owner_id]))[0]}")
    if task.notes:
        details.append(f"Notes:\n{task.notes}")
    return ToolResult("\n".join(details))


_TASK_FIELDS: dict[str, Any] = {
    "title": {"type": "string", "minLength": 1, "maxLength": TITLE_LENGTH},
    "notes": {"type": "string", "maxLength": NOTES_LENGTH},
    "priority": {"type": "integer", "minimum": 1, "maximum": 4},
    "due_at": {
        "type": ["string", "null"],
        "description": "ISO 8601; without an offset it is in the user's time zone",
    },
    "estimate_minutes": {"type": ["integer", "null"], "minimum": 1},
    "recurrence": {
        "type": ["string", "null"],
        "description": "RRULE without DTSTART, e.g. FREQ=WEEKLY;BYDAY=MO; needs due_at",
    },
    "tags": {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": TAG_LENGTH},
        "maxItems": MAX_TAGS,
    },
    "project_id": {"type": ["string", "null"], "description": "null moves it to the inbox"},
}


async def _changes(context: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    zone = await user_zone(context.session, context.user_id)
    changes = {k: v for k, v in args.items() if k in _TASK_FIELDS or k == "status"}
    if "due_at" in changes:
        changes["due_at"] = _when(changes["due_at"], zone)
    if changes.get("project_id") is not None:
        changes["project_id"] = _id(changes, "project_id")
    return changes


async def _create_task(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    changes = await _changes(context, args)
    title = changes.pop("title")
    task = await _service(context).create_task(context.user_id, title, **changes)
    return ToolResult(
        f"Added the task “{task.title}” ({task.id}).",
        Change("task", str(task.id), {}, task_state(task)),
    )


async def _undo_create_task(context: ToolContext, change: Change) -> None:
    task = await _undo_target_task(context, change)
    await drop_task_reminders(context.session, task.id)
    await context.session.delete(task)
    await context.session.flush()


async def _update_task(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _service(context)
    task_id = _id(args, "task_id")
    before = task_state(await service.get_task(context.user_id, task_id))
    task = await service.update_task(context.user_id, task_id, await _changes(context, args))
    return ToolResult(
        f"Updated the task “{task.title}”.",
        Change("task", str(task.id), before, task_state(task)),
    )


async def _undo_task_change(context: ToolContext, change: Change) -> None:
    task = await _undo_target_task(context, change)
    next_id = change.after.get("next_task_id")
    if next_id:
        # The occurrence a completion created goes away with it, if untouched.
        try:
            following = await _service(context).get_task(
                context.user_id, uuid.UUID(next_id), lock=True
            )
        except TasksError:
            raise UndoConflictError("the next occurrence no longer exists") from None
        if task_state(following) != change.after["next_task"]:
            raise UndoConflictError("the next occurrence was changed since")
        await drop_task_reminders(context.session, following.id)
        await context.session.delete(following)
    _restore_task(task, change.before)
    await sync_task_reminder(context.session, task)
    await context.session.flush()


async def _complete_task(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _service(context)
    task_id = _id(args, "task_id")
    before = task_state(await service.get_task(context.user_id, task_id))
    done = await service.complete_task(context.user_id, task_id)
    after = task_state(done.task)
    text = f"Completed “{done.task.title}”."
    if done.next is not None:
        zone = await user_zone(context.session, context.user_id)
        after |= {"next_task_id": str(done.next.id), "next_task": task_state(done.next)}
        text += f" The next one is due {show_local(done.next.due_at, zone)} ({done.next.id})."
    return ToolResult(text, Change("task", str(done.task.id), before, after))


async def _delete_task(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _service(context)
    task_id = _id(args, "task_id")
    task = await service.get_task(context.user_id, task_id)
    before, title = task_state(task), task.title
    await service.delete_task(context.user_id, task_id)
    return ToolResult(f"Deleted the task “{title}”.", Change("task", str(task_id), before, {}))


def _fields_text(args: Mapping[str, Any]) -> str:
    shown = []
    for key in ("title", "status", "priority", "due_at", "recurrence", "project_id", "tags"):
        if key in args:
            value = args[key]
            shown.append(f"{key.replace('_', ' ')} {'none' if value is None else value}")
    if "notes" in args:
        shown.append("notes")
    if "estimate_minutes" in args:
        shown.append(f"estimate {args['estimate_minutes']}")
    return ", ".join(shown) or "nothing"


def _named(title: str | None, fallback: str) -> str:
    return f"“{title}”" if title else fallback


async def _describe_update_task(context: ToolContext, args: Mapping[str, Any]) -> str:
    title = _named(await _task_title(context, args), "a task")
    return f"Change the task {title}: {_fields_text(args)}"


async def _describe_complete_task(context: ToolContext, args: Mapping[str, Any]) -> str:
    return f"Complete the task {_named(await _task_title(context, args), 'a task')}"


async def _describe_delete_task(context: ToolContext, args: Mapping[str, Any]) -> str:
    return f"Delete the task {_named(await _task_title(context, args), 'a task')}"


async def _describe_create_task(context: ToolContext, args: Mapping[str, Any]) -> str:
    project = await _project_title(context, args) if args.get("project_id") else None
    where = f" to the project “{project}”" if project else ""
    return f"Add the task “{args.get('title', '')}”{where}"


_TASK_ID = {"task_id": {"type": "string"}}

LIST_TASKS = ToolSpec(
    domain=DOMAIN,
    name="list_tasks",
    description=(
        "List tasks the user can see, newest first: their own and those of projects "
        "shared with them. Without status, only open tasks (todo, doing). Filters: "
        "project_id, inbox (own tasks without a project), tag, due_before, text "
        "(in the title or notes)."
    ),
    action_class=ActionClass.READ,
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "array",
                "items": {"enum": [s.value for s in TaskStatus]},
                "uniqueItems": True,
            },
            "project_id": {"type": "string"},
            "inbox": {"type": "boolean"},
            "tag": {"type": "string", "maxLength": TAG_LENGTH},
            "due_before": {"type": "string", "description": "ISO 8601"},
            "text": {"type": "string", "minLength": 1, "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
        },
        "additionalProperties": False,
    },
    run=_guard(_list_tasks),
    summarize=lambda args: "List tasks",
)

GET_TASK = ToolSpec(
    domain=DOMAIN,
    name="get_task",
    description="Show one task with its notes.",
    action_class=ActionClass.READ,
    input_schema={
        "type": "object",
        "properties": _TASK_ID,
        "required": ["task_id"],
        "additionalProperties": False,
    },
    run=_guard(_get_task),
    summarize=lambda args: "Show a task",
)

CREATE_TASK = ToolSpec(
    domain=DOMAIN,
    name="create_task",
    description=(
        "Add a task. Without project_id it goes to the user's inbox. Priority 1 is "
        "the most urgent, 4 (the default) the least. A recurring task needs due_at."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _TASK_FIELDS,
        "required": ["title"],
        "additionalProperties": False,
    },
    run=_guard(_create_task),
    summarize=lambda args: f"Add the task “{args.get('title', '')}”",
    undo=_undo_create_task,
    classify=_classify_create_task,
    describe=_describe_create_task,
)

UPDATE_TASK = ToolSpec(
    domain=DOMAIN,
    name="update_task",
    description=(
        "Change a task's fields; only the ones given change, and null clears an "
        "optional one. Status todo or doing reopens it, cancelled closes it; use "
        "complete_task to mark it done."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _TASK_ID
        | _TASK_FIELDS
        | {"status": {"enum": ["todo", "doing", "cancelled"]}},
        "required": ["task_id"],
        "additionalProperties": False,
    },
    run=_guard(_update_task),
    summarize=lambda args: f"Change a task: {_fields_text(args)}",
    undo=_undo_task_change,
    classify=_classify_task_write,
    describe=_describe_update_task,
)

COMPLETE_TASK = ToolSpec(
    domain=DOMAIN,
    name="complete_task",
    description=(
        "Mark a task done. A recurring task gets its next occurrence, which the answer names."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _TASK_ID,
        "required": ["task_id"],
        "additionalProperties": False,
    },
    run=_guard(_complete_task),
    summarize=lambda args: "Complete a task",
    undo=_undo_task_change,
    classify=_classify_task_write,
    describe=_describe_complete_task,
)

DELETE_TASK = ToolSpec(
    domain=DOMAIN,
    name="delete_task",
    description="Delete a task for good. Prefer update_task with status cancelled.",
    action_class=ActionClass.DESTRUCTIVE,
    input_schema={
        "type": "object",
        "properties": _TASK_ID,
        "required": ["task_id"],
        "additionalProperties": False,
    },
    run=_guard(_delete_task),
    summarize=lambda args: "Delete a task",
    describe=_describe_delete_task,
)

# --------------------------------------------------------------- projects


async def _list_projects(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    status = ProjectStatus(args["status"]) if args.get("status") else None
    projects = await _service(context).projects(context.user_id, status=status)
    if not projects:
        return ToolResult("No projects.")
    lines = []
    for shared in projects:
        project = shared.project
        line = f"- {project.id} {project.title}"
        if project.status is ProjectStatus.ARCHIVED:
            line += " [archived]"
        if project.owner_id != context.user_id:
            line += f" · owner {(await _usernames(context, [project.owner_id]))[0]}"
        if shared.shared_with:
            line += f" · shared with {', '.join(await _usernames(context, shared.shared_with))}"
        lines.append(line)
    return ToolResult("Projects:\n" + "\n".join(lines))


async def _create_project(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    shared = await _service(context).create_project(
        context.user_id,
        args["title"],
        description=args.get("description", ""),
        shared_with=await _user_ids(context, args.get("shared_with", ())),
    )
    return ToolResult(
        f"Created the project “{shared.project.title}” ({shared.project.id}).",
        Change("project", str(shared.project.id), {}, await project_state(context, shared)),
    )


async def _undo_target_project(context: ToolContext, change: Change) -> SharedProject:
    project = await context.session.scalar(
        select(Project)
        .where(Project.id == uuid.UUID(change.entity_id), Project.owner_id == context.user_id)
        .with_for_update()
    )
    if project is None:
        raise UndoConflictError("the project no longer exists")
    members = await _service(context).get_project(context.user_id, project.id)
    if await project_state(context, members) != change.after:
        raise UndoConflictError("the project was changed again since")
    return members


async def _undo_create_project(context: ToolContext, change: Change) -> None:
    shared = await _undo_target_project(context, change)
    has_tasks = await context.session.scalar(
        select(Task.id).where(Task.project_id == shared.project.id).limit(1)
    )
    if has_tasks is not None:
        raise UndoConflictError("the project has tasks now")
    await context.session.execute(
        delete(ProjectMember).where(ProjectMember.project_id == shared.project.id)
    )
    await context.session.delete(shared.project)
    await context.session.flush()


async def _update_project(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _service(context)
    project_id = _id(args, "project_id")
    before = await project_state(context, await service.get_project(context.user_id, project_id))
    changes = {k: v for k, v in args.items() if k in ("title", "description", "status")}
    if "shared_with" in args:
        changes["shared_with"] = await _user_ids(context, args["shared_with"])
    shared = await service.update_project(context.user_id, project_id, changes)
    return ToolResult(
        f"Updated the project “{shared.project.title}”.",
        Change("project", str(project_id), before, await project_state(context, shared)),
    )


async def _undo_update_project(context: ToolContext, change: Change) -> None:
    shared = await _undo_target_project(context, change)
    before = change.before
    # The service checks ownership and replaces the members as one change.
    try:
        await _service(context).update_project(
            context.user_id,
            shared.project.id,
            {
                "title": before["title"],
                "description": before["description"],
                "status": before["status"],
                "shared_with": await _user_ids(context, before["shared_with"]),
            },
        )
    except TasksError as exc:
        raise UndoConflictError(str(exc)) from None


async def _delete_project(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _service(context)
    project_id = _id(args, "project_id")
    shared = await service.get_project(context.user_id, project_id)
    before, title = await project_state(context, shared), shared.project.title
    await service.delete_project(context.user_id, project_id)
    return ToolResult(
        f"Deleted the project “{title}”; its tasks moved to their owners' inboxes.",
        Change("project", str(project_id), before, {}),
    )


async def _describe_update_project(context: ToolContext, args: Mapping[str, Any]) -> str:
    title = _named(await _project_title(context, args), "a project")
    shown = [f"{k.replace('_', ' ')} {args[k]}" for k in ("title", "status") if k in args]
    if "description" in args:
        shown.append("description")
    if "shared_with" in args:
        shown.append(f"shared with {', '.join(args['shared_with']) or 'nobody'}")
    return f"Change the project {title}: {', '.join(shown) or 'nothing'}"


async def _describe_delete_project(context: ToolContext, args: Mapping[str, Any]) -> str:
    title = _named(await _project_title(context, args), "a project")
    return f"Delete the project {title}; its tasks move to their owners' inboxes"


def _summarize_create_project(args: Mapping[str, Any]) -> str:
    text = f"Create the project “{args.get('title', '')}”"
    if args.get("shared_with"):
        text += f", shared with {', '.join(args['shared_with'])}"
    return text


_PROJECT_ID = {"project_id": {"type": "string"}}
_USERNAMES = {
    "type": "array",
    "items": {"type": "string", "minLength": 1, "maxLength": 32},
    "maxItems": MAX_SHARED,
    "uniqueItems": True,
    "description": "usernames of household members; see list_members",
}

LIST_PROJECTS = ToolSpec(
    domain=DOMAIN,
    name="list_projects",
    description="List the projects the user owns or that are shared with them.",
    action_class=ActionClass.READ,
    input_schema={
        "type": "object",
        "properties": {"status": {"enum": [s.value for s in ProjectStatus]}},
        "additionalProperties": False,
    },
    run=_guard(_list_projects),
    summarize=lambda args: "List projects",
)

CREATE_PROJECT = ToolSpec(
    domain=DOMAIN,
    name="create_project",
    description=(
        "Create a project. Members it is shared with see and edit its tasks; "
        "sharing asks the user first."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": TITLE_LENGTH},
            "description": {"type": "string", "maxLength": DESCRIPTION_LENGTH},
            "shared_with": _USERNAMES,
        },
        "required": ["title"],
        "additionalProperties": False,
    },
    run=_guard(_create_project),
    summarize=_summarize_create_project,
    undo=_undo_create_project,
    classify=_classify_project_write,
)

UPDATE_PROJECT = ToolSpec(
    domain=DOMAIN,
    name="update_project",
    description=(
        "Rename, describe, archive (status archived) or reactivate a project, or "
        "change who it is shared with (the whole list). Only its owner can."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _PROJECT_ID
        | {
            "title": {"type": "string", "minLength": 1, "maxLength": TITLE_LENGTH},
            "description": {"type": "string", "maxLength": DESCRIPTION_LENGTH},
            "status": {"enum": [s.value for s in ProjectStatus]},
            "shared_with": _USERNAMES,
        },
        "required": ["project_id"],
        "additionalProperties": False,
    },
    run=_guard(_update_project),
    summarize=lambda args: "Change a project",
    undo=_undo_update_project,
    classify=_classify_project_write,
    describe=_describe_update_project,
)

DELETE_PROJECT = ToolSpec(
    domain=DOMAIN,
    name="delete_project",
    description=(
        "Delete a project; its tasks move to their owners' inboxes. Prefer "
        "archiving with update_project."
    ),
    action_class=ActionClass.DESTRUCTIVE,
    input_schema={
        "type": "object",
        "properties": _PROJECT_ID,
        "required": ["project_id"],
        "additionalProperties": False,
    },
    run=_guard(_delete_project),
    summarize=lambda args: "Delete a project",
    describe=_describe_delete_project,
)

TOOLS = (
    LIST_TASKS,
    GET_TASK,
    CREATE_TASK,
    UPDATE_TASK,
    COMPLETE_TASK,
    DELETE_TASK,
    LIST_PROJECTS,
    CREATE_PROJECT,
    UPDATE_PROJECT,
    DELETE_PROJECT,
)
