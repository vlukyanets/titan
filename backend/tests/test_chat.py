import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from titan import tools
from titan.app import MAX_TOOL_ROUNDS, ReplyCut, TooManyToolRounds, create_app

SignIn = Callable[[], dict[str, str]]

KEY = "sk-test-key-1234567890"
URL = "https://models.example/v1"


class FakeModel:
    """Answers as an OpenAI-compatible model does and keeps what it got."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.chunks = ["Hel", "lo"]
        self.done = True
        # Answered instead of a reply; an exception is raised.
        self.response: httpx2.Response | Exception | None = None
        # Tool calls to answer with, one list per request, before the reply.
        self.calls: list[list[tuple[str, dict[str, object]]]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        if self.response is not None:
            return self.response
        events: list[dict[str, object]] = [
            {"choices": [{"delta": {"role": "assistant"}}]}
        ]
        if self.calls:
            for index, (name, args) in enumerate(self.calls.pop(0)):
                # The arguments come in two pieces, as a real stream splits them.
                arguments = json.dumps(args)
                half = len(arguments) // 2
                events += [
                    tool_call(index, f"call-{index}", name, arguments[:half]),
                    tool_call(index, None, None, arguments[half:]),
                ]
        else:
            events += [{"choices": [{"delta": {"content": c}}]} for c in self.chunks]
        body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
        if self.done:
            body += "data: [DONE]\n\n"
        return httpx2.Response(
            200, headers={"Content-Type": "text/event-stream"}, text=body
        )

    def messages(self) -> list[dict[str, str]]:
        """The messages the model got in the last request."""
        result: list[dict[str, str]] = json.loads(self.requests[-1].content)["messages"]
        return result

    def results(self) -> list[object]:
        """The tool results the model got in the last request, parsed."""
        return [
            json.loads(m["content"]) for m in self.messages() if m["role"] == "tool"
        ]


def tool_call(
    index: int, id: str | None, name: str | None, arguments: str
) -> dict[str, object]:
    """A piece of a tool call in a streamed reply."""
    call: dict[str, object] = {"index": index, "function": {"arguments": arguments}}
    if id is not None:
        call.update(
            id=id, type="function", function={"name": name, "arguments": arguments}
        )
    return {"choices": [{"delta": {"tool_calls": [call]}}]}


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def app(migrated: str, key_file: str, model: FakeModel) -> FastAPI:
    return create_app(migrated, key_file, httpx2.MockTransport(model))


@pytest.fixture
def user(client: TestClient, sign_in: SignIn) -> dict[str, str]:
    """A signed-in user whose current model is "big" at URL."""
    headers = sign_in()
    put = client.put(
        "/settings/models/big", json={"url": URL, "key": KEY}, headers=headers
    )
    assert put.status_code == 204
    return headers


def chat(client: TestClient, user: dict[str, str], message: str) -> list[str]:
    """Sends the message and returns the reply as the chunks it came in."""
    with client.stream(
        "POST", "/chat", json={"message": message}, headers=user
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
        return list(response.iter_text())


def test_reply_streams_from_the_current_model(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    assert chat(client, user, "Hi") == ["Hel", "lo"]
    request = model.requests[-1]
    assert str(request.url) == f"{URL}/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {
        "model": "big",
        "messages": [{"role": "user", "content": "Hi"}],
        "tools": tools.SCHEMAS,
        "stream": True,
    }


def test_model_key_the_node_cannot_read_is_named(
    client: TestClient,
    user: dict[str, str],
    model: FakeModel,
    migrated: str,
    tmp_path: Path,
) -> None:
    # The node starts with a new node key, as after its volume was lost.
    new_key = str(tmp_path / "new.key")
    with TestClient(create_app(migrated, new_key, httpx2.MockTransport(model))) as new:
        response = new.post("/chat", json={"message": "Hi"}, headers=user)
    assert response.status_code == 500
    assert response.json() == {
        "detail": 'the key of model "big" cannot be read; add the model again'
    }
    assert model.requests == []


def test_model_without_a_key_gets_no_authorization(
    client: TestClient, sign_in: SignIn, model: FakeModel
) -> None:
    user = sign_in()
    client.put("/settings/models/llama", json={"url": URL}, headers=user)
    chat(client, user, "Hi")
    assert "Authorization" not in model.requests[-1].headers


def test_model_gets_the_earlier_messages(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    chat(client, user, "Hi")
    chat(client, user, "How are you?")
    assert model.messages() == [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": "How are you?"},
    ]


def test_conversation_is_kept_between_runs(
    client: TestClient,
    user: dict[str, str],
    model: FakeModel,
    migrated: str,
    key_file: str,
) -> None:
    chat(client, user, "Hi")
    restarted = create_app(migrated, key_file, httpx2.MockTransport(model))
    with TestClient(restarted) as again:
        chat(again, user, "Still there?")
    assert [m["content"] for m in model.messages()] == ["Hi", "Hello", "Still there?"]


def test_users_do_not_see_each_others_messages(
    client: TestClient, user: dict[str, str], sign_in: SignIn, model: FakeModel
) -> None:
    other = sign_in()
    client.put("/settings/models/big", json={"url": URL}, headers=other)
    chat(client, user, "Mine")
    chat(client, other, "Theirs")
    assert model.messages() == [{"role": "user", "content": "Theirs"}]
    chat(client, user, "Again")
    assert [m["content"] for m in model.messages()] == ["Mine", "Hello", "Again"]


def test_no_current_model_is_409(client: TestClient, sign_in: SignIn) -> None:
    response = client.post("/chat", json={"message": "Hi"}, headers=sign_in())
    assert response.status_code == 409


@pytest.mark.parametrize("message", ["", "  "])
def test_empty_message_is_refused(
    client: TestClient, user: dict[str, str], message: str, model: FakeModel
) -> None:
    response = client.post("/chat", json={"message": message}, headers=user)
    assert response.status_code == 422
    assert model.requests == []


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (httpx2.Response(401), "the model answered 401"),
        # A redirect is not followed: the key would go to that host.
        (
            httpx2.Response(307, headers={"Location": "https://thief.example/v1"}),
            "the model answered 307",
        ),
        (httpx2.ConnectError("refused"), "the model is unreachable"),
    ],
)
def test_model_refusal_is_502_and_nothing_is_kept(
    client: TestClient,
    user: dict[str, str],
    model: FakeModel,
    answer: httpx2.Response | Exception,
    detail: str,
) -> None:
    model.response = answer
    response = client.post("/chat", json={"message": "Lost"}, headers=user)
    assert response.status_code == 502
    assert response.json() == {"detail": detail}
    assert len(model.requests) == 1
    model.response = None
    chat(client, user, "Hi")
    assert model.messages() == [{"role": "user", "content": "Hi"}]


def test_cut_reply_is_not_kept(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    model.done = False
    with pytest.raises(ReplyCut):
        chat(client, user, "Lost")
    model.done = True
    chat(client, user, "Hi")
    assert model.messages() == [{"role": "user", "content": "Hi"}]


def test_logs_never_hold_the_messages_or_the_key(
    client: TestClient,
    user: dict[str, str],
    model: FakeModel,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Every logger at its most verbose, the SQL log of SQLAlchemy too.
    caplog.set_level(logging.DEBUG)
    model.chunks = ["private-", "reply"]
    chat(client, user, "private-question")
    chat(client, user, "next")
    assert caplog.records
    for secret in ["private-question", "private-reply", KEY]:
        assert secret not in caplog.text


def test_tasks_are_added_listed_and_completed_through_tools(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    model.calls = [[("tasks_add", {"title": "Buy milk", "note": "2 l"})]]
    assert chat(client, user, "Add a task to buy milk") == ["Hel", "lo"]
    [milk] = model.results()
    assert isinstance(milk, dict)
    assert milk == {
        "id": milk["id"],
        "title": "Buy milk",
        "note": "2 l",
        "status": "open",
    }

    model.calls = [[("tasks_list", {})]]
    chat(client, user, "What are my tasks?")
    assert model.results() == [[milk]]

    model.calls = [
        [("tasks_update", {"id": milk["id"], "status": "done"})],
        [("tasks_list", {})],
    ]
    chat(client, user, "I bought the milk")
    assert model.results() == [{**milk, "status": "done"}, []]


def test_model_gets_its_tool_calls_and_their_results(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    model.calls = [[("tasks_list", {}), ("tasks_list", {"status": "done"})]]
    chat(client, user, "Anything to do?")
    assert len(model.requests) == 2
    assert model.messages()[1:] == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-0",
                    "type": "function",
                    "function": {"name": "tasks_list", "arguments": "{}"},
                },
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "tasks_list",
                        "arguments": '{"status": "done"}',
                    },
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call-0", "content": "[]"},
        {"role": "tool", "tool_call_id": "call-1", "content": "[]"},
    ]


def test_tool_calls_are_not_kept_in_the_conversation(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    model.calls = [[("tasks_add", {"title": "Call mum"})]]
    chat(client, user, "Remind me to call mum")
    chat(client, user, "Thanks")
    assert model.messages() == [
        {"role": "user", "content": "Remind me to call mum"},
        {"role": "assistant", "content": "Hello"},
        {"role": "user", "content": "Thanks"},
    ]


def test_a_done_task_can_be_opened_again(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    model.calls = [[("tasks_add", {"title": "Water plants"})]]
    chat(client, user, "Water the plants")
    [task] = model.results()
    assert isinstance(task, dict)
    model.calls = [
        [("tasks_update", {"id": task["id"], "status": "done"})],
        [("tasks_update", {"id": task["id"], "status": "open"})],
        [("tasks_list", {})],
    ]
    chat(client, user, "Done, no, not yet")
    assert model.results()[-1] == [task]


@pytest.mark.parametrize(
    "call",
    [
        ("tasks_add", {"title": ""}),
        ("tasks_add", {"title": "   "}),
        ("tasks_add", {}),
    ],
)
def test_an_empty_title_is_refused(
    client: TestClient,
    user: dict[str, str],
    model: FakeModel,
    call: tuple[str, dict[str, object]],
) -> None:
    model.calls = [[call], [("tasks_list", {})]]
    chat(client, user, "Add a task")
    [refused, tasks] = model.results()
    assert isinstance(refused, dict) and refused["error"]
    assert tasks == []


def test_an_empty_title_is_refused_when_changed(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    model.calls = [[("tasks_add", {"title": "Pay rent"})]]
    chat(client, user, "Add a task")
    [task] = model.results()
    assert isinstance(task, dict)
    model.calls = [
        [("tasks_update", {"id": task["id"], "title": " "})],
        [("tasks_list", {})],
    ]
    chat(client, user, "Rename it")
    [refused, tasks] = model.results()
    assert isinstance(refused, dict) and refused["error"]
    assert tasks == [task]


def test_a_title_in_any_script_is_kept_unchanged(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    title = "Купить молоко 🥛 牛乳を買う مرحبا"
    model.calls = [[("tasks_add", {"title": title})], [("tasks_list", {})]]
    chat(client, user, title)
    [added, tasks] = model.results()
    assert isinstance(added, dict) and added["title"] == title
    assert tasks == [added]
    assert title in model.messages()[-1]["content"]


def test_unknown_and_other_users_task_are_the_same_not_found(
    client: TestClient, user: dict[str, str], sign_in: SignIn, model: FakeModel
) -> None:
    model.calls = [[("tasks_add", {"title": "Mine"})]]
    chat(client, user, "Add mine")
    [mine] = model.results()
    assert isinstance(mine, dict)

    other = sign_in()
    client.put("/settings/models/big", json={"url": URL}, headers=other)
    model.calls = [
        [
            ("tasks_update", {"id": mine["id"], "status": "done"}),
            ("tasks_update", {"id": tools.LARGEST_ID, "status": "done"}),
        ]
    ]
    chat(client, other, "Complete it")
    assert model.results() == [{"error": "not found"}, {"error": "not found"}]

    model.calls = [[("tasks_list", {})]]
    chat(client, user, "What are my tasks?")
    assert model.results() == [[mine]]


def test_an_id_too_large_for_the_database_is_refused(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    model.calls = [[("tasks_update", {"id": tools.LARGEST_ID + 1, "status": "done"})]]
    assert chat(client, user, "Complete it") == ["Hel", "lo"]
    [refused] = model.results()
    assert isinstance(refused, dict) and refused["error"]


def test_listing_no_tasks_gives_an_empty_list(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    model.calls = [[("tasks_list", {}), ("tasks_list", {"status": "done"})]]
    chat(client, user, "What are my tasks?")
    assert model.results() == [[], []]


@pytest.mark.parametrize(
    "call",
    [("tasks_delete", {}), ("tasks_list", {"status": "someday"})],
)
def test_a_call_the_tools_cannot_take_goes_back_as_an_error(
    client: TestClient,
    user: dict[str, str],
    model: FakeModel,
    call: tuple[str, dict[str, object]],
) -> None:
    model.calls = [[call]]
    assert chat(client, user, "Do it") == ["Hel", "lo"]
    [refused] = model.results()
    assert isinstance(refused, dict) and refused["error"]


def test_cut_reply_undoes_its_tool_calls(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    model.calls = [[("tasks_add", {"title": "Lost"})]]
    model.done = False
    with pytest.raises(ReplyCut):
        chat(client, user, "Add a task")
    model.done = True
    model.calls = [[("tasks_list", {})]]
    chat(client, user, "What are my tasks?")
    assert model.results() == [[]]
    assert model.messages()[0] == {"role": "user", "content": "What are my tasks?"}


def test_a_model_stuck_calling_tools_is_stopped(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    model.calls = [[("tasks_add", {"title": "Again"})]] * (MAX_TOOL_ROUNDS + 1)
    with pytest.raises(TooManyToolRounds):
        chat(client, user, "Add it")
    assert len(model.requests) == MAX_TOOL_ROUNDS + 1
    model.calls = [[("tasks_list", {})]]
    chat(client, user, "What are my tasks?")
    assert model.results() == [[]]
