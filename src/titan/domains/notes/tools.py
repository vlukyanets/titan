"""Agent tools of notes and memory (docs/spec/domains/notes-memory.md#agent-tools).

Every call goes through `NotesService` or `MemoryService` with `commit=False`.
A write to a note shared with another user is `external`. Memories are never
shared, so their tools never are.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

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
from titan.domains.calendar.zones import show_local, user_zone
from titan.domains.notes.errors import InvalidMemoryError, InvalidNoteError, NotesError
from titan.domains.notes.models import Memory, MemorySource
from titan.domains.notes.service import (
    BODY_LENGTH,
    MAX_PAGE,
    MAX_SHARED,
    MAX_TAGS,
    QUERY_LENGTH,
    STATEMENT_LENGTH,
    TAG_LENGTH,
    TITLE_LENGTH,
    MemoryService,
    NoteQuery,
    NotesService,
    SharedNote,
)

DEFAULT_LIST = 20
# A long note is cut here, so one call cannot fill the model's context.
SHOWN_BODY = 20_000

# ------------------------------------------------------------------ helpers


def _notes(context: ToolContext) -> NotesService:
    return NotesService(context.session, commit=False)


def _memory(context: ToolContext) -> MemoryService:
    return MemoryService(context.session, commit=False)


def _guard(run: RunFn) -> RunFn:
    return guarded(run, (NotesError,))


def _id(args: Mapping[str, Any], key: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(args[key]))
    except ValueError:
        raise InvalidNoteError(f"{key} is not an id") from None


async def _reader_ids(context: ToolContext, usernames: Iterable[object]) -> list[uuid.UUID]:
    return await member_ids(context.session, usernames, InvalidNoteError)


async def note_state(context: ToolContext, shared: SharedNote) -> dict[str, Any]:
    note = shared.note
    return {
        "title": note.title,
        "body": note.body,
        "tags": list(note.tags),
        "shared_with": sorted(await member_names(context.session, shared.shared_with)),
    }


def memory_state(memory: Memory) -> dict[str, Any]:
    return {
        "statement": memory.statement,
        "confidence": memory.confidence,
        "last_confirmed_at": memory.last_confirmed_at.isoformat(),
    }


def _named(note: SharedNote | None) -> str:
    if note is None:
        return "a note"
    return f"“{note.note.title}”" if note.note.title else "an untitled note"


async def _note_or_none(context: ToolContext, args: Mapping[str, Any]) -> SharedNote | None:
    try:
        return await _notes(context).get_note(context.user_id, uuid.UUID(str(args["note_id"])))
    except (NotesError, ValueError, KeyError):
        return None


# ------------------------------------------------------------------ notes


async def _search_notes(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    shared = args.get("shared")
    found = await _notes(context).notes(
        context.user_id,
        NoteQuery(
            text=args.get("text"),
            tag=args.get("tag"),
            mine=None if shared is None else shared == "mine",
        ),
        limit=int(args.get("limit", DEFAULT_LIST)),
    )
    if not found:
        return ToolResult("No notes match.")
    owners = dict(
        zip(
            [n.owner_id for n in found],
            await member_names(context.session, [n.owner_id for n in found]),
            strict=True,
        )
    )
    lines = []
    for note in found:
        line = f"- {note.id} {note.title or '(untitled)'}"
        if note.owner_id != context.user_id:
            line += f" · by {owners[note.owner_id]}"
        if note.tags:
            line += " · " + " ".join(f"#{t}" for t in note.tags)
        if note.excerpt:
            line += f"\n  {note.excerpt}"
        lines.append(line)
    return ToolResult("Notes, most recently changed first:\n" + "\n".join(lines))


async def _get_note(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    shared = await _notes(context).get_note(context.user_id, _id(args, "note_id"))
    note = shared.note
    zone = await user_zone(context.session, context.user_id)
    head = [f"{note.title or '(untitled)'} · changed {show_local(note.updated_at, zone)}"]
    if note.owner_id != context.user_id:
        head.append(f"by {(await member_names(context.session, [note.owner_id]))[0]}")
    if note.tags:
        head.append(" ".join(f"#{t}" for t in note.tags))
    body = note.body
    if len(body) > SHOWN_BODY:
        body = body[:SHOWN_BODY] + f"\n[… cut; the note has {len(note.body)} characters]"
    return ToolResult(" · ".join(head) + "\n\n" + body)


async def _create_note(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    shared = await _notes(context).create_note(
        context.user_id,
        title=args.get("title", ""),
        body=args.get("body", ""),
        tags=args.get("tags", ()),
        shared_with=await _reader_ids(context, args.get("shared_with", ())),
    )
    return ToolResult(
        f"Saved the note {_named(shared)} ({shared.note.id}).",
        Change("note", str(shared.note.id), {}, await note_state(context, shared)),
    )


async def _unchanged_note(context: ToolContext, change: Change) -> SharedNote:
    try:
        shared = await _notes(context).get_note(context.user_id, uuid.UUID(change.entity_id))
    except NotesError:
        raise UndoConflictError("the note no longer exists") from None
    if await note_state(context, shared) != change.after:
        raise UndoConflictError("the note was changed again since")
    return shared


async def _undo_create_note(context: ToolContext, change: Change) -> None:
    shared = await _unchanged_note(context, change)
    await _notes(context).delete_note(context.user_id, shared.note.id)


async def _update_note(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _notes(context)
    note_id = _id(args, "note_id")
    before = await note_state(context, await service.get_note(context.user_id, note_id))
    changes = {k: v for k, v in args.items() if k in ("title", "body", "tags")}
    if "shared_with" in args:
        changes["shared_with"] = await _reader_ids(context, args["shared_with"])
    shared = await service.update_note(context.user_id, note_id, changes)
    return ToolResult(
        f"Updated the note {_named(shared)}.",
        Change("note", str(note_id), before, await note_state(context, shared)),
    )


async def _undo_update_note(context: ToolContext, change: Change) -> None:
    shared = await _unchanged_note(context, change)
    before = change.before
    try:
        await _notes(context).update_note(
            context.user_id,
            shared.note.id,
            {
                "title": before["title"],
                "body": before["body"],
                "tags": before["tags"],
                "shared_with": await _reader_ids(context, before["shared_with"]),
            },
        )
    except NotesError as exc:
        raise UndoConflictError(str(exc)) from None


async def _delete_note(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _notes(context)
    note_id = _id(args, "note_id")
    shared = await service.get_note(context.user_id, note_id)
    before, name = await note_state(context, shared), _named(shared)
    await service.delete_note(context.user_id, note_id)
    return ToolResult(f"Deleted the note {name}.", Change("note", str(note_id), before, {}))


async def _classify_note_write(context: ToolContext, args: Mapping[str, Any]) -> ActionClass:
    shared = bool(args.get("shared_with"))
    if not shared and "note_id" in args:
        note = await _note_or_none(context, args)
        shared = note is not None and (
            bool(note.shared_with) or note.note.owner_id != context.user_id
        )
    return ActionClass.EXTERNAL if shared else ActionClass.WRITE_INTERNAL


def _summarize_create_note(args: Mapping[str, Any]) -> str:
    text = f"Save a note “{args.get('title') or str(args.get('body', ''))[:60]}”"
    if args.get("shared_with"):
        text += f", shared with {', '.join(args['shared_with'])}"
    return text


async def _describe_update_note(context: ToolContext, args: Mapping[str, Any]) -> str:
    changed = [k.replace("_", " ") for k in args if k != "note_id"]
    if args.get("shared_with") is not None:
        changed[changed.index("shared with")] = (
            f"shared with {', '.join(args['shared_with']) or 'nobody'}"
        )
    note = await _note_or_none(context, args)
    return f"Change the note {_named(note)}: {', '.join(changed) or 'nothing'}"


async def _describe_delete_note(context: ToolContext, args: Mapping[str, Any]) -> str:
    return f"Delete the note {_named(await _note_or_none(context, args))}"


_USERNAMES = {
    "type": "array",
    "items": {"type": "string", "minLength": 1, "maxLength": 32},
    "maxItems": MAX_SHARED,
    "uniqueItems": True,
    "description": "usernames of household members who can read it; see list_members",
}
_NOTE_FIELDS: dict[str, Any] = {
    "title": {"type": "string", "maxLength": TITLE_LENGTH},
    "body": {"type": "string", "maxLength": BODY_LENGTH, "description": "Markdown"},
    "tags": {
        "type": "array",
        "items": {"type": "string", "minLength": 1, "maxLength": TAG_LENGTH},
        "maxItems": MAX_TAGS,
    },
    "shared_with": _USERNAMES,
}
_NOTE_ID = {"note_id": {"type": "string"}}

SEARCH_NOTES = ToolSpec(
    domain="notes",
    name="search_notes",
    description=(
        "Find notes the user owns or that are shared with them, most recently "
        "changed first, with an excerpt. text matches notes containing every word, "
        "also inside longer words, in any language."
    ),
    action_class=ActionClass.READ,
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": QUERY_LENGTH},
            "tag": {"type": "string", "maxLength": TAG_LENGTH},
            "shared": {"enum": ["mine", "with_me"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
        },
        "additionalProperties": False,
    },
    run=_guard(_search_notes),
    summarize=lambda args: "Search notes",
)

GET_NOTE = ToolSpec(
    domain="notes",
    name="get_note",
    description="Read a whole note.",
    action_class=ActionClass.READ,
    input_schema={
        "type": "object",
        "properties": _NOTE_ID,
        "required": ["note_id"],
        "additionalProperties": False,
    },
    run=_guard(_get_note),
    summarize=lambda args: "Read a note",
)

CREATE_NOTE = ToolSpec(
    domain="notes",
    name="create_note",
    description="Save a note (Markdown) with a title, a body or both.",
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={"type": "object", "properties": _NOTE_FIELDS, "additionalProperties": False},
    run=_guard(_create_note),
    summarize=_summarize_create_note,
    undo=_undo_create_note,
    classify=_classify_note_write,
)

UPDATE_NOTE = ToolSpec(
    domain="notes",
    name="update_note",
    description=(
        "Change a note the user owns; only the fields given change. shared_with "
        "replaces the whole list of readers."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _NOTE_ID | _NOTE_FIELDS,
        "required": ["note_id"],
        "additionalProperties": False,
    },
    run=_guard(_update_note),
    summarize=lambda args: "Change a note",
    undo=_undo_update_note,
    classify=_classify_note_write,
    describe=_describe_update_note,
)

DELETE_NOTE = ToolSpec(
    domain="notes",
    name="delete_note",
    description="Delete a note the user owns.",
    action_class=ActionClass.DESTRUCTIVE,
    input_schema={
        "type": "object",
        "properties": _NOTE_ID,
        "required": ["note_id"],
        "additionalProperties": False,
    },
    run=_guard(_delete_note),
    summarize=lambda args: "Delete a note",
    describe=_describe_delete_note,
)

# ----------------------------------------------------------------- memory


async def _recall(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    found = await _memory(context).memories(
        context.user_id, text=args.get("text"), limit=int(args.get("limit", DEFAULT_LIST))
    )
    if not found:
        return ToolResult("Nothing remembered matches.")
    lines = [f"- {m.id} {m.statement} (confidence {m.confidence:.2f})" for m in found]
    return ToolResult("Remembered, newest first:\n" + "\n".join(lines))


async def _remember(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _memory(context)
    if args.get("note_id"):
        source, source_id = MemorySource.NOTE, _id(args, "note_id")
    elif context.thread_id is not None:
        source, source_id = MemorySource.CHAT, context.thread_id
    else:
        raise InvalidMemoryError("a memory needs the note it comes from")
    statement = str(args["statement"])
    existing = await service.same(context.user_id, " ".join(statement.split()))
    before = memory_state(existing) if existing is not None else {}
    memory = await service.remember(
        context.user_id,
        statement,
        source=source,
        source_id=source_id,
        confidence=float(args.get("confidence", 0.8)),
    )
    text = "Confirmed what I already knew" if before else "Remembered"
    return ToolResult(
        f"{text}: {memory.statement} ({memory.id}).",
        Change("memory", str(memory.id), before, memory_state(memory)),
    )


async def _unchanged_memory(context: ToolContext, change: Change) -> Memory:
    try:
        memory = await _memory(context).get_memory(
            context.user_id, uuid.UUID(change.entity_id), lock=True
        )
    except NotesError:
        raise UndoConflictError("the memory no longer exists") from None
    if memory_state(memory) != change.after:
        raise UndoConflictError("the memory was changed again since")
    return memory


def _restore_memory(memory: Memory, state: Mapping[str, Any]) -> None:
    memory.statement = state["statement"]
    memory.confidence = state["confidence"]
    memory.last_confirmed_at = datetime.fromisoformat(state["last_confirmed_at"])
    memory.updated_at = datetime.now(memory.last_confirmed_at.tzinfo)


async def _undo_memory_change(context: ToolContext, change: Change) -> None:
    memory = await _unchanged_memory(context, change)
    if change.before:
        _restore_memory(memory, change.before)
    else:
        await context.session.delete(memory)
    await context.session.flush()


async def _revise(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _memory(context)
    memory_id = _id(args, "memory_id")
    before = memory_state(await service.get_memory(context.user_id, memory_id))
    memory = await service.update_memory(context.user_id, memory_id, str(args["statement"]))
    return ToolResult(
        f"Now remembered as: {memory.statement}.",
        Change("memory", str(memory_id), before, memory_state(memory)),
    )


async def _forget(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    service = _memory(context)
    memory_id = _id(args, "memory_id")
    memory = await service.get_memory(context.user_id, memory_id)
    before = memory_state(memory)
    await service.forget(context.user_id, memory_id)
    return ToolResult(
        f"Forgot: {before['statement']}.", Change("memory", str(memory_id), before, {})
    )


async def _describe_memory(context: ToolContext, args: Mapping[str, Any]) -> str | None:
    try:
        memory = await _memory(context).get_memory(
            context.user_id, uuid.UUID(str(args["memory_id"]))
        )
    except (NotesError, ValueError, KeyError):
        return None
    if "statement" in args:
        return f"Change the memory “{memory.statement}” to “{args['statement']}”"
    return f"Forget “{memory.statement}”"


_STATEMENT = {"type": "string", "minLength": 1, "maxLength": STATEMENT_LENGTH}
_MEMORY_ID = {"memory_id": {"type": "string"}}

RECALL = ToolSpec(
    domain="memory",
    name="recall",
    description=(
        "Look up what you remember about the user: facts, preferences, people. "
        "text keeps statements containing every word."
    ),
    action_class=ActionClass.READ,
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 1, "maxLength": QUERY_LENGTH},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE},
        },
        "additionalProperties": False,
    },
    run=_guard(_recall),
    summarize=lambda args: "Recall memories",
)

REMEMBER = ToolSpec(
    domain="memory",
    name="remember",
    description=(
        "Remember a short, lasting fact about the user, in one sentence, such as "
        "“Anna is allergic to peanuts”. Only for what they would want kept. A fact "
        "already known is confirmed, not stored twice. It comes from this "
        "conversation, or from note_id when it was read in a note."
    ),
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": {
            "statement": _STATEMENT,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "note_id": {"type": "string"},
        },
        "required": ["statement"],
        "additionalProperties": False,
    },
    run=_guard(_remember),
    summarize=lambda args: f"Remember: {args.get('statement', '')}",
    undo=_undo_memory_change,
)

REVISE_MEMORY = ToolSpec(
    domain="memory",
    name="revise_memory",
    description="Correct a remembered statement, as the user put it.",
    action_class=ActionClass.WRITE_INTERNAL,
    input_schema={
        "type": "object",
        "properties": _MEMORY_ID | {"statement": _STATEMENT},
        "required": ["memory_id", "statement"],
        "additionalProperties": False,
    },
    run=_guard(_revise),
    summarize=lambda args: f"Change a memory to: {args.get('statement', '')}",
    undo=_undo_memory_change,
    describe=_describe_memory,
)

FORGET = ToolSpec(
    domain="memory",
    name="forget",
    description="Forget a remembered statement for good.",
    action_class=ActionClass.DESTRUCTIVE,
    input_schema={
        "type": "object",
        "properties": _MEMORY_ID,
        "required": ["memory_id"],
        "additionalProperties": False,
    },
    run=_guard(_forget),
    summarize=lambda args: "Forget a memory",
    describe=_describe_memory,
)

TOOLS = (
    SEARCH_NOTES,
    GET_NOTE,
    CREATE_NOTE,
    UPDATE_NOTE,
    DELETE_NOTE,
    RECALL,
    REMEMBER,
    REVISE_MEMORY,
    FORGET,
)
