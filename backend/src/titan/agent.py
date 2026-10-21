"""The agent: the model plans the actions of a message, and the node runs them.

The idea comes from LLMCompiler (arXiv 2312.04511); the code is our own.
"""

import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from importlib import resources
from typing import Annotated, Any, Literal

import httpx2
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)
from pydantic.fields import FieldInfo
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from titan import db, tools

log = logging.getLogger(__name__)

# Steps in one plan; a longer plan is refused before it runs.
MAX_PLAN_STEPS = 20
# Plans the model may make for one message; after the last one fails, the
# model replies that nothing changed.
MAX_PLAN_ATTEMPTS = 3

PROMPTS = resources.files("titan").joinpath("prompts")
SYSTEM_PROMPT = PROMPTS.joinpath("system.md").read_text()
# The system prompt of a think step; {answer} says what to answer.
THINK_PROMPT = PROMPTS.joinpath("think.md").read_text()

NOTHING_SAVED = "Nothing was saved: the database did not take the changes."

# A reference to a step's result, such as "$s1.items.0.id", or to "$item".
REFERENCE = re.compile(r"\$([A-Za-z_]\w*)((?:\.\w+)*)", re.ASCII)

Message = dict[str, Any]
Call = dict[str, Any]
# Sends the messages to the model, with these tools or none, and returns
# its streamed response.
Ask = Callable[[list[Message], list[dict[str, Any]] | None], Awaitable[httpx2.Response]]


class ReplyCut(Exception):
    """The model's stream ended before it said the reply was done."""


class PlanFailed(Exception):
    """A plan refused before it ran, or a step that failed; says why."""


StepId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class Do(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StepId
    do: str
    args: dict[str, Any] = Field(default_factory=dict)


class ForEach(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StepId
    for_each: str
    do: str
    args: dict[str, Any] = Field(default_factory=dict)


class Typed(BaseModel):
    """An answer of one JSON type."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["integer", "number", "string", "boolean"]


class Choose(BaseModel):
    """An answer that is this field of one item of the input, such as its id."""

    model_config = ConfigDict(extra="forbid")

    choose: str


class Think(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: StepId
    think: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    input: Any = None
    output: Typed | Choose


Step = Do | ForEach | Think


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: Annotated[list[Step], Field(min_length=1, max_length=MAX_PLAN_STEPS)]


def _tool_line(name: str, args: type[BaseModel], description: str) -> str:
    schema = args.model_json_schema()
    shape = {"properties": schema["properties"], "required": schema.get("required", [])}
    return f"- {name}: {description} Arguments: {json.dumps(shape)}"


PLAN_TOOL = "run_plan"

RUN_PLAN = {
    "type": "function",
    "function": {
        "name": PLAN_TOOL,
        "description": "Run a plan of steps that read and change the user's data."
        " The tools a step can call:\n"
        + "\n".join(_tool_line(n, a, d) for n, (a, _, d) in tools.TOOLS.items()),
        "parameters": Plan.model_json_schema(),
    },
}


def data(value: object) -> str:
    """The value as JSON in a marked block for the model.

    "<" is escaped, so nothing inside can close the block.
    """
    text = json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")
    return f"<data>\n{text}\n</data>"


def conversation(history: list[db.Message], question: db.Message) -> list[Message]:
    """The messages of the first model call: the system prompt, then the
    user's conversation."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *({"role": m.role, "content": m.content} for m in [*history, question]),
    ]


def _references(value: object) -> Iterator[re.Match[str]]:
    if isinstance(value, str) and (match := REFERENCE.fullmatch(value)):
        yield match
    elif isinstance(value, dict):
        for item in value.values():
            yield from _references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _references(item)


def _fits(field: FieldInfo, value: object) -> None:
    annotation: Any = field.annotation
    if field.metadata:
        annotation = Annotated[(annotation, *field.metadata)]
    TypeAdapter(annotation).validate_python(value)


def check(plan: Plan) -> None:
    """Refuses a plan that cannot run, before any step runs."""
    above: set[str] = set()
    for step in plan.steps:
        where = f"step {step.id}"
        if step.id in above or step.id == "item":
            raise PlanFailed(f"{where}: the id {step.id!r} is taken")
        if isinstance(step, Think):
            _check_references(where, step.input, above)
            above.add(step.id)
            continue
        if step.do not in tools.TOOLS:
            raise PlanFailed(f"{where}: no tool {step.do!r}")
        fields = tools.TOOLS[step.do][0].model_fields
        if unknown := sorted(set(step.args) - set(fields)):
            raise PlanFailed(f"{where}: {step.do} takes no argument {unknown[0]!r}")
        required = {name for name, field in fields.items() if field.is_required()}
        if missing := sorted(required - set(step.args)):
            raise PlanFailed(f"{where}: {step.do} needs the argument {missing[0]!r}")
        for name, value in step.args.items():
            # A value read from another step is checked when the step runs.
            if next(_references(value), None) is None:
                try:
                    _fits(fields[name], value)
                except ValidationError as error:
                    raise PlanFailed(
                        f"{where}: {name}: {tools.problems(error)}"
                    ) from None
        known = above
        if isinstance(step, ForEach):
            if not REFERENCE.fullmatch(step.for_each):
                raise PlanFailed(
                    f"{where}: for_each takes a reference, such as $s1.items"
                )
            _check_references(where, step.for_each, above)
            known = above | {"item"}
        _check_references(where, step.args, known)
        above.add(step.id)


def _check_references(where: str, value: object, known: set[str]) -> None:
    for match in _references(value):
        if match[1] not in known:
            raise PlanFailed(f"{where}: {match[0]} reads no step above it")


def resolve(value: object, results: dict[str, object], item: object = None) -> object:
    """The value with each reference replaced by what it reads."""
    if isinstance(value, str) and (match := REFERENCE.fullmatch(value)):
        found = item if match[1] == "item" else results[match[1]]
        for part in match[2].split(".")[1:]:
            if isinstance(found, dict) and part in found:
                found = found[part]
            elif isinstance(found, list) and part.isdigit() and int(part) < len(found):
                found = found[int(part)]
            else:
                raise PlanFailed(f"{match[0]}: there is no {part!r}")
        return found
    if isinstance(value, dict):
        return {key: resolve(v, results, item) for key, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, results, item) for v in value]
    return value


_ANSWER_TYPES: dict[str, tuple[type, ...]] = {
    "integer": (int,),
    "number": (int, float),
    "string": (str,),
    "boolean": (bool,),
}


async def think(step: Think, input: object, ask: Ask) -> object:
    """Asks the model the step's question about its input; returns the
    answer, checked against the step's output."""
    choices: list[object] = []
    if isinstance(step.output, Choose):
        field = step.output.choose
        if not isinstance(input, list) or not all(
            isinstance(item, dict) and field in item for item in input
        ):
            raise PlanFailed(f"choose needs a list of items that have {field!r}")
        choices = [item[field] for item in input]
        what = f'the "{field}" of the one item of the data that answers it'
    else:
        what = f"a JSON {step.output.type}"
    messages = [
        {"role": "system", "content": THINK_PROMPT.replace("{answer}", what)},
        {"role": "user", "content": f"{step.think}\n\n{data(input)}"},
    ]
    said: list[str] = []
    async for _ in read_reply(await ask(messages, None), {}, said):
        pass
    text = "".join(said).strip().removeprefix("```json").strip("`").strip()
    try:
        answer = json.loads(text)
    except json.JSONDecodeError:
        raise PlanFailed("the answer is not JSON") from None
    if isinstance(step.output, Choose):
        # Types compared too: true is not the id 1.
        if not any(type(answer) is type(c) and answer == c for c in choices):
            raise PlanFailed(f"the answer is no item's {step.output.choose!r}")
        return answer
    expected = step.output.type
    # A bool is an int in Python, but not an integer in JSON.
    if isinstance(answer, bool) != (expected == "boolean") or not isinstance(
        answer, _ANSWER_TYPES[expected]
    ):
        raise PlanFailed(f"the answer is not of type {expected}")
    return answer


async def run(user_id: int, plan: Plan, ask: Ask) -> dict[str, object]:
    """Runs the steps in order and returns the result of each."""
    results: dict[str, object] = {}
    for step in plan.steps:
        try:
            if isinstance(step, Think):
                results[step.id] = await think(step, resolve(step.input, results), ask)
            elif isinstance(step, ForEach):
                items = resolve(step.for_each, results)
                if not isinstance(items, list):
                    raise PlanFailed(f"{step.for_each} is not a list")
                results[step.id] = {
                    "items": [
                        await tools.call(
                            user_id, step.do, resolve(step.args, results, i)
                        )
                        for i in items
                    ]
                }
            else:
                results[step.id] = await tools.call(
                    user_id, step.do, resolve(step.args, results)
                )
        except (PlanFailed, tools.ToolError) as error:
            raise PlanFailed(f"step {step.id}: {error}") from None
        except SQLAlchemyError:
            raise PlanFailed(f"step {step.id}: the database refused it") from None
    return results


async def attempt(user_id: int, call: Call, ask: Ask) -> tuple[bool, str]:
    """Checks and runs a plan the model sent; returns whether it ran, and
    the result for the model. A plan that fails changes nothing."""
    function = call["function"]
    if function["name"] != PLAN_TOOL:
        return False, data({"error": f"no tool {function['name']!r}; call {PLAN_TOOL}"})
    started = time.monotonic()
    try:
        plan = Plan.model_validate_json(function["arguments"] or "{}")
        check(plan)
        async with db.savepoint():
            results = await run(user_id, plan, ask)
    except ValidationError as error:
        log.info("a plan was refused: it does not fit the schema")
        return False, data({"error": f"the plan does not fit: {tools.problems(error)}"})
    except PlanFailed as error:
        log.info("a plan failed; nothing of it was kept")
        return False, data({"error": str(error), "changed": "nothing"})
    log.info(
        "a plan of %d steps ran in %.3f s", len(plan.steps), time.monotonic() - started
    )
    return True, data({"results": results})


def _assistant(said: list[str], calls: dict[int, Call]) -> Message:
    return {
        "role": "assistant",
        "content": "".join(said) or None,
        "tool_calls": list(calls.values()),
    }


def _result(call: Call, content: str) -> Message:
    return {"role": "tool", "tool_call_id": call["id"], "content": content}


async def read_reply(
    response: httpx2.Response, calls: dict[int, Call], said: list[str]
) -> AsyncIterator[str]:
    """Yields the text of a streamed reply, keeps it in said, and gathers its
    tool calls.

    A tool call comes in pieces; calls keeps each one, by its index, in the
    form the model takes back.
    """
    try:
        async for event in httpx2.EventSource(response):
            if event.data == "[DONE]":
                return
            for choice in json.loads(event.data)["choices"]:
                delta = choice["delta"]
                if text := delta.get("content"):
                    said.append(text)
                    yield text
                for piece in delta.get("tool_calls") or []:
                    call = calls.setdefault(
                        piece["index"],
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    call["id"] += piece.get("id") or ""
                    function = piece.get("function") or {}
                    for field in ("name", "arguments"):
                        call["function"][field] += function.get(field) or ""
        # Raising cuts the connection, so the CLI sees it too.
        raise ReplyCut
    finally:
        await response.aclose()


async def converse(
    engine: AsyncEngine,
    user_id: int,
    question: db.Message,
    messages: list[Message],
    ask: Ask,
    response: httpx2.Response,
) -> AsyncIterator[str]:
    """Streams the reply to the question, from the first model response on.

    A plan is built whole before it runs, and nothing changes it while it
    runs. A plan that fails changes nothing, and the model may send a new
    one, for at most MAX_PLAN_ATTEMPTS plans. The changes of the message
    commit before the reply that tells of them; the question and the reply
    are kept only when the reply is whole.
    """
    said: list[str] = []
    # Whether the model still has to write the reply: after a plan, not
    # after a text answer.
    to_answer = False
    try:
        async with db.transaction(engine):
            for attempts in range(1, MAX_PLAN_ATTEMPTS + 1):
                calls: dict[int, Call] = {}
                before = len(said)
                async for text in read_reply(response, calls, said):
                    yield text
                to_answer = bool(calls)
                if not calls:
                    break
                messages.append(_assistant(said[before:], calls))
                ran = True
                for call in calls.values():
                    done, result = await attempt(user_id, call, ask)
                    ran = ran and done
                    messages.append(_result(call, result))
                if ran:
                    break
                if attempts == MAX_PLAN_ATTEMPTS:
                    log.warning("no plan of a message ran")
                    break
                response = await ask(messages, [RUN_PLAN])
    except db.CommitFailed:
        log.exception("the changes of a message were not saved")
        yield f"\n\n{NOTHING_SAVED}" if said else NOTHING_SAVED
        return
    if to_answer:
        # The results or the last error are in; the model writes the reply
        # and calls nothing.
        async for text in read_reply(await ask(messages, None), {}, said):
            yield text
    async with db.transaction(engine):
        answer = db.Message("assistant", "".join(said))
        await db.add_messages(user_id, question, answer)
