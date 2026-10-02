"""Agent tools of the calendar and the planner (docs/spec/domains/calendar.md#agent-tools).

Every call goes through `CalendarService` with `commit=False`. Creating or
changing an event with attendees is `external`, so the default policy asks
first. The planner places time blocks with `planner.place`, so working hours,
buffers and existing events hold whatever the model asks for.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from titan.domains.accounts.tools import member_ids, member_names
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
from titan.domains.calendar import planner
from titan.domains.calendar.errors import CalendarError, InvalidEventError
from titan.domains.calendar.models import EventKind
from titan.domains.calendar.service import (
    DESCRIPTION_LENGTH,
    LOCATION_LENGTH,
    MAX_ATTENDEES,
    TITLE_LENGTH,
    CalendarService,
    SharedEvent,
    merge,
    zone,
)
from titan.domains.tasks.errors import TasksError
from titan.domains.tasks.models import TaskStatus
from titan.domains.tasks.service import TaskQuery, TasksService

DEFAULT_DAYS = 7
# A task without an estimate gets a block this long.
DEFAULT_BLOCK = timedelta(minutes=30)
MAX_PLANNED = 20
# How far ahead replanning looks for a free slot.
REPLAN_DAYS = 7

# ------------------------------------------------------------------ helpers


def _service(context: ToolContext) -> CalendarService:
    return CalendarService(context.session, commit=False)


def _guard(run: RunFn) -> RunFn:
    return guarded(run, (CalendarError, TasksError))


def _id(args: Mapping[str, Any], key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(args[key]))
    except ValueError:
        raise InvalidEventError(f"{key} is not an id") from None


def _time(value: object, tz: ZoneInfo) -> datetime:
    """ISO 8601 date or date and time; a date is its local midnight."""
    text = str(value)
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), time(0), tz)
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise InvalidEventError(f"{value} is not an ISO 8601 date or time") from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def _day(value: object, tz: ZoneInfo, now: datetime) -> date:
    if value is None:
        return now.astimezone(tz).date()
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise InvalidEventError(f"{value} is not a date (YYYY-MM-DD)") from None


def _show(value: datetime, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%a %Y-%m-%d %H:%M")


def _span(begins: datetime, ends: datetime, tz: ZoneInfo) -> str:
    local_end = ends.astimezone(tz)
    same_day = local_end.date() == begins.astimezone(tz).date()
    return f"{_show(begins, tz)} to {local_end.strftime('%H:%M') if same_day else _show(ends, tz)}"


async def _tz(context: ToolContext) -> ZoneInfo:
    return zone((await _service(context).prefs(context.user_id)).time_zone)


async def _hours(context: ToolContext) -> planner.Hours:
    prefs = await _service(context).prefs(context.user_id)
    return planner.Hours(
        zone(prefs.time_zone),
        prefs.work_start,
        prefs.work_end,
        list(prefs.work_days),
        timedelta(minutes=prefs.buffer_minutes),
    )


async def event_state(context: ToolContext, shared: SharedEvent) -> dict[str, Any]:
    event = shared.event
    return {
        "title": event.title,
        "description": event.description,
        "location": event.location,
        "starts_at": event.starts_at.astimezone(UTC).isoformat(),
        "ends_at": event.ends_at.astimezone(UTC).isoformat(),
        "all_day": event.all_day,
        "time_zone": event.time_zone,
        "recurrence": event.recurrence,
        "attendees": sorted(await member_names(context.session, shared.attendees)),
        "task_id": str(event.task_id) if event.task_id else None,
    }


async def _restore(context: ToolContext, event_id: uuid.UUID, state: Mapping[str, Any]) -> None:
    changes = dict(state)
    for name in ("starts_at", "ends_at"):
        changes[name] = datetime.fromisoformat(state[name])
    changes["attendees"] = await member_ids(context.session, state["attendees"], InvalidEventError)
    try:
        await _service(context).update_event(context.user_id, event_id, changes)
    except CalendarError as exc:
        raise UndoConflictError(str(exc)) from None


async def _unchanged(context: ToolContext, event_id: str, after: Mapping[str, Any]) -> SharedEvent:
    try:
        shared = await _service(context).get_event(context.user_id, uuid.UUID(event_id))
    except CalendarError:
        raise UndoConflictError("the event no longer exists") from None
    if shared.event.owner_id != context.user_id or await event_state(context, shared) != after:
        raise UndoConflictError("the event was changed again since")
    return shared


async def _event_or_none(context: ToolContext, args: Mapping[str, Any]) -> SharedEvent | None:
    try:
        return await _service(context).get_event(context.user_id, uuid.UUID(str(args["event_id"])))
    except (CalendarError, ValueError, KeyError):
        return None


# --------------------------------------------------------------- reading


def _window(args: Mapping[str, Any], tz: ZoneInfo) -> tuple[datetime, datetime]:
    now = datetime.now(tz)
    start = (
        _time(args["start"], tz) if args.get("start") else datetime.combine(now.date(), time(0), tz)
    )
    end = _time(args["end"], tz) if args.get("end") else start + timedelta(days=DEFAULT_DAYS)
    return start, end


async def _list_events(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    tz = await _tz(context)
    start, end = _window(args, tz)
    found = await _service(context).occurrences(context.user_id, start, end)
    if not found:
        return ToolResult(f"Nothing between {_show(start, tz)} and {_show(end, tz)}.")
    lines = []
    for occurrence in found:
        event = occurrence.event
        if event.all_day:
            when = f"{occurrence.starts_at.astimezone(zone(event.time_zone)):%a %Y-%m-%d} all day"
        else:
            when = _span(occurrence.starts_at, occurrence.ends_at, tz)
        line = f"- {event.id} {when} {event.title}"
        if event.kind is EventKind.TIME_BLOCK:
            line += f" [time block for task {event.task_id}]"
        if event.location:
            line += f" · at {event.location}"
        if event.owner_id != context.user_id:
            line += f" · by {(await member_names(context.session, [event.owner_id]))[0]}"
        if event.recurrence:
            line += f" · repeats {event.recurrence}"
        lines.append(line)
    return ToolResult(f"Calendar ({tz}):\n" + "\n".join(lines))


async def _free_busy(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    hours = await _hours(context)
    tz = hours.zone
    start, end = _window(args, tz)
    busy = await _service(context).busy(context.user_id, start, end)
    now = datetime.now(UTC)
    lines = [f"Free within working hours, with a {hours.buffer.seconds // 60} min buffer ({tz}):"]
    day = start.astimezone(tz).date()
    while datetime.combine(day, time(0), tz) < end:
        slots = planner.free_slots(day, hours, busy, now)
        if slots:
            gaps = ", ".join(
                f"{a.astimezone(tz):%H:%M} to {b.astimezone(tz):%H:%M}" for a, b in slots
            )
            lines.append(f"- {day:%a %Y-%m-%d}: {gaps}")
        day += timedelta(days=1)
    lines.append("Busy:")
    lines.extend(f"- {_span(a, b, tz)}" for a, b in busy)
    return ToolResult("\n".join(lines))


# ----------------------------------------------------------------- events

_EVENT_FIELDS: dict[str, Any] = {
    "title": {"type": "string", "minLength": 1, "maxLength": TITLE_LENGTH},
    "starts_at": {
        "type": "string",
        "description": "ISO 8601; without an offset in the user's zone",
    },
    "ends_at": {"type": "string"},
    "all_day": {"type": "boolean"},
    "description": {"type": "string", "maxLength": DESCRIPTION_LENGTH},
    "location": {"type": ["string", "null"], "maxLength": LOCATION_LENGTH},
    "recurrence": {
        "type": ["string", "null"],
        "description": "RRULE without DTSTART, e.g. FREQ=WEEKLY;BYDAY=MO",
    },
    "attendees": {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": 32},
        "maxItems": MAX_ATTENDEES,
        "uniqueItems": True,
        "description": "usernames of household members; see list_members",
    },
}


async def _event_changes(context: ToolContext, args: Mapping[str, Any]) -> dict[str, Any]:
    tz = await _tz(context)
    changes = {k: v for k, v in args.items() if k in _EVENT_FIELDS}
    for name in ("starts_at", "ends_at"):
        if name in changes:
            changes[name] = _time(changes[name], tz)
    if "attendees" in changes:
        changes["attendees"] = await member_ids(
            context.session, changes["attendees"], InvalidEventError
        )
    return changes


async def _create_event(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    changes = await _event_changes(context, args)
    shared = await _service(context).create_event(
        context.user_id,
        changes.pop("title"),
        changes.pop("starts_at"),
        changes.pop("ends_at"),
        **changes,
    )
    tz = await _tz(context)
    return ToolResult(
        f"Added “{shared.event.title}”, {_span(shared.event.starts_at, shared.event.ends_at, tz)}"
        f" ({shared.event.id}).",
        Change("event", str(shared.event.id), {}, await event_state(context, shared)),
    )


async def _undo_create(context: ToolContext, change: Change) -> None:
    shared = await _unchanged(context, change.entity_id, change.after)
    await _service(context).delete_event(context.user_id, shared.event.id)


async def _update_event(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _service(context)
    event_id = _id(args, "event_id")
    before = await event_state(context, await service.get_event(context.user_id, event_id))
    shared = await service.update_event(
        context.user_id, event_id, await _event_changes(context, args)
    )
    tz = await _tz(context)
    return ToolResult(
        f"Changed “{shared.event.title}”, now "
        f"{_span(shared.event.starts_at, shared.event.ends_at, tz)}.",
        Change("event", str(event_id), before, await event_state(context, shared)),
    )


async def _undo_update(context: ToolContext, change: Change) -> None:
    shared = await _unchanged(context, change.entity_id, change.after)
    await _restore(context, shared.event.id, change.before)


async def _delete_event(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _service(context)
    event_id = _id(args, "event_id")
    shared = await service.get_event(context.user_id, event_id)
    before, title = await event_state(context, shared), shared.event.title
    await service.delete_event(context.user_id, event_id)
    return ToolResult(f"Deleted “{title}”.", Change("event", str(event_id), before, {}))


async def _classify_event_write(context: ToolContext, args: Mapping[str, Any]) -> ActionClass:
    shared = bool(args.get("attendees"))
    if not shared and "event_id" in args:
        found = await _event_or_none(context, args)
        shared = found is not None and bool(found.attendees)
    return ActionClass.EXTERNAL if shared else ActionClass.WRITE_INTERNAL


async def _describe_update(context: ToolContext, args: Mapping[str, Any]) -> str | None:
    found = await _event_or_none(context, args)
    if found is None:
        return None
    changed = [k.replace("_", " ") for k in args if k != "event_id"]
    return f"Change “{found.event.title}”: {', '.join(changed) or 'nothing'}"


async def _describe_delete(context: ToolContext, args: Mapping[str, Any]) -> str | None:
    found = await _event_or_none(context, args)
    return None if found is None else f"Delete “{found.event.title}” from the calendar"


def _summarize_create(args: Mapping[str, Any]) -> str:
    text = f"Add “{args.get('title', '')}” at {args.get('starts_at', '?')}"
    if args.get("attendees"):
        text += f" with {', '.join(args['attendees'])}"
    return text


# ---------------------------------------------------------------- planner


async def _plan_day(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    hours = await _hours(context)
    tz = hours.zone
    now = datetime.now(UTC)
    day = _day(args.get("date"), tz, now)
    window = planner.working_window(day, hours)
    if window is None:
        return ToolResult(f"{day:%A %Y-%m-%d} is not a working day; nothing was planned.")
    tasks = TasksService(context.session, commit=False)
    if args.get("task_ids"):
        wanted = [
            await tasks.get_task(context.user_id, uuid.UUID(str(t))) for t in args["task_ids"]
        ]
    else:
        open_tasks = await tasks.tasks(
            context.user_id, TaskQuery(statuses=[TaskStatus.TODO, TaskStatus.DOING]), limit=100
        )
        far = datetime.max.replace(tzinfo=UTC)
        wanted = sorted(
            (t for t in open_tasks if t.owner_id == context.user_id),
            key=lambda t: (t.due_at or far, t.priority, t.id),
        )
    wanted = [t for t in wanted if t.status in (TaskStatus.TODO, TaskStatus.DOING)]
    service = _service(context)
    blocked = {
        o.event.task_id
        for o in await service.occurrences(context.user_id, *window)
        if o.event.kind is EventKind.TIME_BLOCK
    }
    wanted = [t for t in wanted if t.id not in blocked][:MAX_PLANNED]
    busy = await service.busy(context.user_id, *window)
    lengths = [
        timedelta(minutes=t.estimate_minutes) if t.estimate_minutes else DEFAULT_BLOCK
        for t in wanted
    ]
    placed = planner.place(lengths, planner.free_slots(day, hours, busy, now), hours.buffer)
    blocks, lines, left = [], [], []
    for task, slot in zip(wanted, placed, strict=True):
        if slot is None:
            left.append(task.title)
            continue
        shared = await service.create_event(
            context.user_id,
            task.title,
            slot[0],
            slot[1],
            kind=EventKind.TIME_BLOCK,
            task_id=task.id,
        )
        blocks.append({"id": str(shared.event.id)} | await event_state(context, shared))
        lines.append(f"- {_span(slot[0], slot[1], tz)} {task.title} ({shared.event.id})")
    if not blocks:
        if not left:
            return ToolResult("Nothing was planned: no open tasks without a block that day.")
        return ToolResult("Nothing was planned. No room for: " + ", ".join(left))
    text = f"Planned {day:%A %Y-%m-%d}:\n" + "\n".join(lines)
    if left:
        text += "\nNo room for: " + ", ".join(left)
    return ToolResult(text, Change("plan", f"{context.user_id}:{day}", {}, {"blocks": blocks}))


async def _undo_plan(context: ToolContext, change: Change) -> None:
    for block in change.after["blocks"]:
        state = {k: v for k, v in block.items() if k != "id"}
        shared = await _unchanged(context, block["id"], state)
        await _service(context).delete_event(context.user_id, shared.event.id)


async def _replan_block(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _service(context)
    block_id = _id(args, "event_id")
    shared = await service.get_event(context.user_id, block_id)
    block = shared.event
    if block.kind is not EventKind.TIME_BLOCK or block.owner_id != context.user_id:
        raise InvalidEventError("only your own time blocks are replanned")
    if block.task_id is not None:
        task = await TasksService(context.session, commit=False).get_task(
            context.user_id, block.task_id
        )
        if task.status not in (TaskStatus.TODO, TaskStatus.DOING):
            raise InvalidEventError("its task is closed; nothing to replan")
    hours = await _hours(context)
    now = datetime.now(UTC)
    length = block.ends_at - block.starts_at
    first = max(now, block.ends_at).astimezone(hours.zone).date()
    for offset in range(REPLAN_DAYS):
        day = first + timedelta(days=offset)
        window = planner.working_window(day, hours)
        if window is None:
            continue
        # Everything but the block itself, which is about to move.
        busy = merge(
            (o.starts_at, o.ends_at)
            for o in await service.occurrences(context.user_id, *window)
            if o.event.id != block.id
        )
        (slot,) = planner.place([length], planner.free_slots(day, hours, busy, now), hours.buffer)
        if slot is not None:
            break
    else:
        return ToolResult(f"No free slot in the next {REPLAN_DAYS} days.", is_error=True)
    before = await event_state(context, shared)
    moved = await service.update_event(
        context.user_id, block_id, {"starts_at": slot[0], "ends_at": slot[1]}
    )
    return ToolResult(
        f"Moved “{block.title}” to {_span(slot[0], slot[1], hours.zone)}.",
        Change("event", str(block_id), before, await event_state(context, moved)),
    )


_EVENT_ID = {"event_id": {"type": "string"}}

LIST_EVENTS = ToolSpec(
    domain="calendar",
    name="list_events",
    description=(
        "The user's calendar between start and end (ISO dates or times, by default "
        "the next 7 days): their events, the ones they attend, and time blocks."
    ),
    action_class=ActionClass.READ,
    input_schema={
        "type": "object",
        "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
        "additionalProperties": False,
    },
    run=_guard(_list_events),
    summarize=lambda args: "Show the calendar",
)

FREE_BUSY = ToolSpec(
    domain="calendar",
    name="free_busy",
    description=(
        "Free time within the user's working hours, keeping their buffer, and busy "
        "intervals, between start and end (by default the next 7 days)."
    ),
    action_class=ActionClass.READ,
    input_schema={
        "type": "object",
        "properties": {"start": {"type": "string"}, "end": {"type": "string"}},
        "additionalProperties": False,
    },
    run=_guard(_free_busy),
    summarize=lambda args: "Find free time",
)

CREATE_EVENT = ToolSpec(
    domain="calendar",
    name="create_event",
    description=(
        "Add an event to the user's calendar. Inviting attendees asks the user "
        "first. An all-day event covers whole local days."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _EVENT_FIELDS,
        "required": ["title", "starts_at", "ends_at"],
        "additionalProperties": False,
    },
    run=_guard(_create_event),
    summarize=_summarize_create,
    undo=_undo_create,
    classify=_classify_event_write,
)

UPDATE_EVENT = ToolSpec(
    domain="calendar",
    name="update_event",
    description=(
        "Change or move an event the user owns; only the fields given change. "
        "attendees replaces the whole list. A recurring event changes as a series."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _EVENT_ID | _EVENT_FIELDS,
        "required": ["event_id"],
        "additionalProperties": False,
    },
    run=_guard(_update_event),
    summarize=lambda args: "Change an event",
    undo=_undo_update,
    classify=_classify_event_write,
    describe=_describe_update,
)

DELETE_EVENT = ToolSpec(
    domain="calendar",
    name="delete_event",
    description="Delete an event the user owns, or a whole series.",
    action_class=ActionClass.DESTRUCTIVE,
    input_schema={
        "type": "object",
        "properties": _EVENT_ID,
        "required": ["event_id"],
        "additionalProperties": False,
    },
    run=_guard(_delete_event),
    summarize=lambda args: "Delete an event",
    describe=_describe_delete,
)

PLAN_DAY = ToolSpec(
    domain="calendar",
    name="plan_day",
    description=(
        "Place time blocks for open tasks on a working day (by default today), "
        "inside working hours, around existing events with the buffer. Without "
        "task_ids it takes the user's open tasks by due date, then priority; with "
        "them, those tasks in that order. A task without an estimate gets 30 minutes. "
        "Tasks that already have a block that day are skipped."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": {
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_PLANNED,
                "uniqueItems": True,
            },
        },
        "additionalProperties": False,
    },
    run=_guard(_plan_day),
    summarize=lambda args: f"Plan {args.get('date', 'today')}",
    undo=_undo_plan,
)

REPLAN_BLOCK = ToolSpec(
    domain="calendar",
    name="replan_block",
    description=(
        "Move a time block whose task is not done to the next free slot of the "
        "same length, from now or its end on."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _EVENT_ID,
        "required": ["event_id"],
        "additionalProperties": False,
    },
    run=_guard(_replan_block),
    summarize=lambda args: "Move a time block to a free slot",
    undo=_undo_update,
)

TOOLS = (
    LIST_EVENTS,
    FREE_BUSY,
    CREATE_EVENT,
    UPDATE_EVENT,
    DELETE_EVENT,
    PLAN_DAY,
    REPLAN_BLOCK,
)
