"""The tools the model may call, and how the node runs them."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, ValidationError

from titan import db

# Task ids are Postgres integers; a larger one would fail the whole message.
LARGEST_ID = 2**31 - 1

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Status = Literal["open", "done"]


class TasksAdd(BaseModel):
    title: Title
    note: str | None = None


class TasksList(BaseModel):
    status: Status = "open"


class TasksUpdate(BaseModel):
    id: Annotated[int, Field(ge=1, le=LARGEST_ID)]
    title: Title | None = None
    note: str | None = None
    status: Status | None = None


async def tasks_add(user_id: int, args: TasksAdd) -> object:
    return asdict(await db.add_task(user_id, args.title, args.note))


async def tasks_list(user_id: int, args: TasksList) -> object:
    return [asdict(t) for t in await db.tasks_of(user_id, args.status)]


async def tasks_update(user_id: int, args: TasksUpdate) -> object:
    changes = args.model_dump(exclude={"id"}, exclude_none=True)
    task = await db.update_task(user_id, args.id, changes)
    # The same answer for another user's task, so ids reveal nothing.
    return {"error": "not found"} if task is None else asdict(task)


Run = Callable[[int, Any], Awaitable[object]]

TOOLS: dict[str, tuple[type[BaseModel], Run, str]] = {
    "tasks_add": (TasksAdd, tasks_add, "Add a task for the user."),
    "tasks_list": (
        TasksList,
        tasks_list,
        "List the user's tasks of a status, open by default.",
    ),
    "tasks_update": (
        TasksUpdate,
        tasks_update,
        "Change a task's title, note or status: done completes it, open reopens it.",
    ),
}

# The tools as the OpenAI chat completions API takes them.
SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": args.model_json_schema(),
        },
    }
    for name, (args, _, description) in TOOLS.items()
]


async def run(user_id: int, name: str, arguments: str) -> str:
    """Runs a tool call of the model for the user and returns its result.

    A call the tool cannot take gets an error the model can read and fix.
    """
    if name not in TOOLS:
        return json.dumps({"error": f"no tool {name!r}"})
    args, call, _ = TOOLS[name]
    try:
        parsed = args.model_validate_json(arguments or "{}")
    except ValidationError as error:
        problems = error.errors(include_url=False, include_input=False)
        return json.dumps(
            {"error": [f"{'.'.join(map(str, p['loc']))}: {p['msg']}" for p in problems]}
        )
    return json.dumps(await call(user_id, parsed), ensure_ascii=False)
