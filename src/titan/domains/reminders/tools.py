"""Agent tools of reminders (docs/spec/domains/reminders.md#agent-tools).

Every call goes through `RemindersService` with `commit=False`. Reminders are
private to their owner, so no call is `external`.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

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
from titan.domains.reminders.errors import InvalidReminderError, RemindersError
from titan.domains.reminders.models import LinkType, Reminder, ReminderStatus
from titan.domains.reminders.service import (
    DEFAULT_SNOOZE_MINUTES,
    MAX_PAGE,
    MAX_SNOOZE_MINUTES,
    TEXT_LENGTH,
    RemindersService,
)

DOMAIN = "reminders"
DEFAULT_LIST = 30
_TIMES = ("fire_at", "occurs_at")


def _service(context: ToolContext) -> RemindersService:
    return RemindersService(context.session, commit=False)


def _guard(run: RunFn) -> RunFn:
    return guarded(run, (RemindersError,))


def _id(args: Mapping[str, Any], key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(args[key]))
    except ValueError:
        raise InvalidReminderError(f"{key} is not an id") from None


def _when(value: object, zone: ZoneInfo) -> datetime:
    try:
        return parse_local(str(value), zone)
    except ValueError:
        raise InvalidReminderError(f"{value} is not an ISO 8601 date and time") from None


def reminder_state(reminder: Reminder) -> dict[str, Any]:
    return {
        "text": reminder.text,
        "fire_at": reminder.fire_at.astimezone(UTC).isoformat(),
        "occurs_at": reminder.occurs_at.astimezone(UTC).isoformat(),
        "recurrence": reminder.recurrence,
        "status": reminder.status.value,
    }


def _line(reminder: Reminder, zone: ZoneInfo) -> str:
    parts = [f"- {reminder.id} {reminder.text}", f"fires {show_local(reminder.fire_at, zone)}"]
    if reminder.recurrence:
        parts.append(f"repeats {reminder.recurrence}")
    if reminder.status is not ReminderStatus.SCHEDULED:
        parts.append(reminder.status.value)
    if reminder.link_type is not None:
        parts.append(f"for {reminder.link_type.value} {reminder.link_id}")
    if reminder.is_default:
        parts.append("default")
    return " · ".join(parts)


async def _list(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    status = ReminderStatus(args["status"]) if args.get("status") else None
    found = await _service(context).reminders(
        context.user_id, status=status, limit=int(args.get("limit", DEFAULT_LIST))
    )
    if not found:
        return ToolResult("No reminders.")
    zone = await user_zone(context.session, context.user_id)
    return ToolResult("Reminders, newest first:\n" + "\n".join(_line(r, zone) for r in found))


async def _create(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    zone = await user_zone(context.session, context.user_id)
    task_id = _id(args, "task_id") if args.get("task_id") else None
    reminder = await _service(context).create(
        context.user_id,
        args["text"],
        _when(args["fire_at"], zone),
        recurrence=args.get("recurrence"),
        link_type=LinkType.TASK if task_id else None,
        link_id=task_id,
    )
    return ToolResult(
        f"I will remind you {show_local(reminder.fire_at, zone)}: {reminder.text} ({reminder.id}).",
        Change("reminder", str(reminder.id), {}, reminder_state(reminder)),
    )


async def _update(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _service(context)
    reminder_id = _id(args, "reminder_id")
    before = reminder_state(await service.get(context.user_id, reminder_id))
    changes = {k: v for k, v in args.items() if k in ("text", "fire_at", "recurrence")}
    zone = await user_zone(context.session, context.user_id)
    if "fire_at" in changes:
        changes["fire_at"] = _when(changes["fire_at"], zone)
    reminder = await service.update(context.user_id, reminder_id, changes)
    return ToolResult(
        f"Changed: {reminder.text}, fires {show_local(reminder.fire_at, zone)}.",
        Change("reminder", str(reminder.id), before, reminder_state(reminder)),
    )


async def _snooze(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _service(context)
    reminder_id = _id(args, "reminder_id")
    before = reminder_state(await service.get(context.user_id, reminder_id))
    snoozed = await service.snooze(
        context.user_id, reminder_id, int(args.get("minutes", DEFAULT_SNOOZE_MINUTES))
    )
    zone = await user_zone(context.session, context.user_id)
    # A recurring reminder is snoozed as a one-off copy; undo removes the copy.
    if snoozed.id != reminder_id:
        before = {}
    return ToolResult(
        f"Snoozed until {show_local(snoozed.fire_at, zone)}.",
        Change("reminder", str(snoozed.id), before, reminder_state(snoozed)),
    )


async def _undo(context: ToolContext, change: Change) -> None:
    try:
        reminder = await _service(context).get(
            context.user_id, uuid.UUID(change.entity_id), lock=True
        )
    except RemindersError:
        raise UndoConflictError("the reminder no longer exists") from None
    if reminder_state(reminder) != change.after:
        raise UndoConflictError("the reminder was changed or has fired since")
    if not change.before:
        await context.session.delete(reminder)
    else:
        before = change.before
        reminder.text = before["text"]
        for name in _TIMES:
            setattr(reminder, name, datetime.fromisoformat(before[name]))
        reminder.recurrence = before["recurrence"]
        reminder.status = ReminderStatus(before["status"])
        reminder.updated_at = datetime.now(UTC)
    await context.session.flush()


async def _delete(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _service(context)
    reminder_id = _id(args, "reminder_id")
    reminder = await service.get(context.user_id, reminder_id)
    before, text = reminder_state(reminder), reminder.text
    await service.delete(context.user_id, reminder_id)
    return ToolResult(
        f"Deleted the reminder: {text}.", Change("reminder", str(reminder_id), before, {})
    )


async def _describe(context: ToolContext, args: Mapping[str, Any]) -> str | None:
    try:
        reminder = await _service(context).get(context.user_id, uuid.UUID(str(args["reminder_id"])))
    except (RemindersError, ValueError, KeyError):
        return None
    return (
        f"Change the reminder “{reminder.text}”: {', '.join(k for k in args if k != 'reminder_id')}"
    )


async def _describe_delete(context: ToolContext, args: Mapping[str, Any]) -> str | None:
    try:
        reminder = await _service(context).get(context.user_id, uuid.UUID(str(args["reminder_id"])))
    except (RemindersError, ValueError, KeyError):
        return None
    return f"Delete the reminder “{reminder.text}”"


_FIRE_AT = {
    "type": "string",
    "description": "ISO 8601; without an offset it is in the user's time zone",
}
_RULE = {
    "type": ["string", "null"],
    "description": "RRULE without DTSTART, e.g. FREQ=DAILY; repeats from fire_at",
}
_REMINDER_ID = {"reminder_id": {"type": "string"}}

LIST_REMINDERS = ToolSpec(
    domain=DOMAIN,
    name="list_reminders",
    description="List the user's reminders, newest first.",
    action_class=ActionClass.READ,
    input_schema={
        "type": "object",
        "properties": {
            "status": {"enum": [s.value for s in ReminderStatus]},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
        },
        "additionalProperties": False,
    },
    run=_guard(_list),
    summarize=lambda args: "List reminders",
)

CREATE_REMINDER = ToolSpec(
    domain=DOMAIN,
    name="create_reminder",
    description=(
        "Remind the user at a time: a push notification with the text. It can "
        "repeat, and can belong to one of their tasks (task_id)."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": TEXT_LENGTH},
            "fire_at": _FIRE_AT,
            "recurrence": _RULE,
            "task_id": {"type": "string"},
        },
        "required": ["text", "fire_at"],
        "additionalProperties": False,
    },
    run=_guard(_create),
    summarize=lambda args: f"Remind at {args.get('fire_at', '?')}: {args.get('text', '')}",
    undo=_undo,
)

UPDATE_REMINDER = ToolSpec(
    domain=DOMAIN,
    name="update_reminder",
    description=(
        "Change a reminder's text, time or repeat rule; a new time or rule starts "
        "it again from fire_at, and recurrence null stops the series."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _REMINDER_ID
        | {
            "text": {"type": "string", "minLength": 1, "maxLength": TEXT_LENGTH},
            "fire_at": _FIRE_AT,
            "recurrence": _RULE,
        },
        "required": ["reminder_id"],
        "additionalProperties": False,
    },
    run=_guard(_update),
    summarize=lambda args: "Change a reminder",
    undo=_undo,
    describe=_describe,
)

SNOOZE_REMINDER = ToolSpec(
    domain=DOMAIN,
    name="snooze_reminder",
    description=f"Make a reminder fire again later, {DEFAULT_SNOOZE_MINUTES} minutes by default.",
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _REMINDER_ID
        | {"minutes": {"type": "integer", "minimum": 1, "maximum": MAX_SNOOZE_MINUTES}},
        "required": ["reminder_id"],
        "additionalProperties": False,
    },
    run=_guard(_snooze),
    summarize=lambda args: (
        f"Snooze a reminder for {args.get('minutes', DEFAULT_SNOOZE_MINUTES)} min"
    ),
    undo=_undo,
)

DELETE_REMINDER = ToolSpec(
    domain=DOMAIN,
    name="delete_reminder",
    description="Delete a reminder, or a whole repeating series.",
    action_class=ActionClass.DESTRUCTIVE,
    input_schema={
        "type": "object",
        "properties": _REMINDER_ID,
        "required": ["reminder_id"],
        "additionalProperties": False,
    },
    run=_guard(_delete),
    summarize=lambda args: "Delete a reminder",
    describe=_describe_delete,
)

TOOLS = (LIST_REMINDERS, CREATE_REMINDER, UPDATE_REMINDER, SNOOZE_REMINDER, DELETE_REMINDER)
