import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from fake_model import KEY, URL, FakeModel, chat
from fastapi.testclient import TestClient

from titan import agent
from titan.agent import ReplyCut
from titan.app import create_app

SignIn = Callable[[], dict[str, str]]


def test_reply_streams_from_the_current_model(
    client: TestClient, user: dict[str, str], model: FakeModel
) -> None:
    assert chat(client, user, "Hi") == ["Hel", "lo"]
    request = model.requests[-1]
    assert str(request.url) == f"{URL}/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {
        "model": "big",
        "messages": [
            {"role": "system", "content": agent.SYSTEM_PROMPT},
            {"role": "user", "content": "Hi"},
        ],
        "tools": [agent.RUN_PLAN],
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
    model.script = [
        [
            (
                "run_plan",
                {
                    "steps": [
                        {
                            "id": "s1",
                            "do": "tasks_add",
                            "args": {"title": "private-task"},
                        }
                    ]
                },
            )
        ]
    ]
    model.chunks = ["private-", "reply"]
    chat(client, user, "private-question")
    chat(client, user, "next")
    assert caplog.records
    for secret in ["private-question", "private-reply", "private-task", KEY]:
        assert secret not in caplog.text
