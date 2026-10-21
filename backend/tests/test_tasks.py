"""Tasks, kept through the tools the plans of the model call."""

from collections.abc import Callable

import pytest
from fake_model import URL, FakeModel, chat, plan
from fastapi.testclient import TestClient

from titan import tools

SignIn = Callable[[], dict[str, str]]


def step(id: str, do: str, /, **args: object) -> dict[str, object]:
    return {"id": id, "do": do, "args": args}


def ran(model: FakeModel) -> dict[str, object]:
    """The results of the plan the model sent last, by step."""
    [result] = model.results()
    assert isinstance(result, dict), result
    results = result["results"]
    assert isinstance(results, dict)
    return results


def refused(model: FakeModel) -> str:
    """The error the model got for the plan it sent last."""
    [result] = model.results()
    assert isinstance(result, dict) and result["changed"] == "nothing", result
    error = result["error"]
    assert isinstance(error, str)
    return error


def tasks(client: TestClient, user: dict[str, str], model: FakeModel) -> object:
    """The user's open tasks, read by a plan of its own."""
    model.script = [plan(step("s1", "tasks_list"))]
    chat(client, user, "What are my tasks?")
    return ran(model)["s1"]


def add(
    client: TestClient, user: dict[str, str], model: FakeModel, title: str
) -> dict[str, object]:
    model.script = [plan(step("s1", "tasks_add", title=title))]
    chat(client, user, f"Add {title}")
    task = ran(model)["s1"]
    assert isinstance(task, dict)
    return task


def test_tasks_are_added_listed_and_completed(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    model.script = [plan(step("s1", "tasks_add", title="Buy milk", note="2 l"))]
    assert chat(client, user, "Add a task to buy milk") == ["Hel", "lo"]
    milk = ran(model)["s1"]
    assert isinstance(milk, dict)
    assert milk == {
        "id": milk["id"],
        "title": "Buy milk",
        "note": "2 l",
        "status": "open",
    }

    assert tasks(client, user, model) == {"items": [milk]}

    model.script = [
        plan(
            step("s1", "tasks_update", id=milk["id"], status="done"),
            step("s2", "tasks_list"),
        )
    ]
    chat(client, user, "I bought the milk")
    assert ran(model) == {"s1": {**milk, "status": "done"}, "s2": {"items": []}}


def test_a_done_task_can_be_opened_again(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    task = add(client, user, model, "Water plants")
    model.script = [
        plan(
            step("s1", "tasks_update", id=task["id"], status="done"),
            step("s2", "tasks_update", id=task["id"], status="open"),
        )
    ]
    chat(client, user, "Done, no, not yet")
    assert tasks(client, user, model) == {"items": [task]}


@pytest.mark.parametrize("args", [{"title": ""}, {"title": "   "}, {}])
def test_an_empty_title_is_refused(
    client: TestClient, user: dict[str, str], model: FakeModel, args: dict[str, object]
) -> None:
    model.script = [plan({"id": "s1", "do": "tasks_add", "args": args})]
    chat(client, user, "Add a task")
    assert "title" in refused(model)
    assert tasks(client, user, model) == {"items": []}


def test_an_empty_title_is_refused_when_changed(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    task = add(client, user, model, "Pay rent")
    model.script = [plan(step("s1", "tasks_update", id=task["id"], title=" "))]
    chat(client, user, "Rename it")
    assert "title" in refused(model)
    assert tasks(client, user, model) == {"items": [task]}


def test_a_title_in_any_script_is_kept_unchanged(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    title = "Купить молоко 🥛 牛乳を買う مرحبا"
    task = add(client, user, model, title)
    assert task["title"] == title
    assert tasks(client, user, model) == {"items": [task]}


def test_unknown_and_other_users_task_are_the_same_not_found(
    client: TestClient, user: dict[str, str], sign_in: SignIn, model: FakeModel
) -> None:
    mine = add(client, user, model, "Mine")
    other = sign_in()
    client.put("/settings/models/big", json={"url": URL}, headers=other)
    errors = []
    for id in [mine["id"], tools.LARGEST_ID]:
        model.script = [plan(step("s1", "tasks_update", id=id, status="done"))]
        chat(client, other, "Complete it")
        errors.append(refused(model))
    assert errors == ["step s1: not found"] * 2
    assert tasks(client, user, model) == {"items": [mine]}


def test_an_id_too_large_for_the_database_is_refused(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    model.script = [
        plan(step("s1", "tasks_update", id=tools.LARGEST_ID + 1, status="done"))
    ]
    assert chat(client, user, "Complete it") == ["Hel", "lo"]
    assert "id" in refused(model)


def test_listing_no_tasks_gives_an_empty_list(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    model.script = [
        plan(step("s1", "tasks_list"), step("s2", "tasks_list", status="done"))
    ]
    chat(client, user, "What are my tasks?")
    assert ran(model) == {"s1": {"items": []}, "s2": {"items": []}}


def test_tool_calls_are_not_kept_in_the_conversation(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    add(client, user, model, "Call mum")
    chat(client, user, "Thanks")
    assert model.messages() == [
        {"role": "user", "content": "Add Call mum"},
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": "Thanks"},
    ]
