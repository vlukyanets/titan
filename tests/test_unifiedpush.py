"""Endpoint checks and the UnifiedPush sender, against a mocked push server."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from titan.notify import PushResult, UnifiedPushSender, endpoint_allowed, new_client, origin_of
from titan.settings import Settings

URL = "postgresql+psycopg://user:pw@db/titan"


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://100.64.0.1:8080/upAbC?up=1", "http://100.64.0.1:8080"),
        ("HTTPS://Ntfy.Example.TS.net:443/x", "https://ntfy.example.ts.net"),
        ("http://push.test:80", "http://push.test"),
        ("http://[fd7a:115c::1]:8080/up", "http://[fd7a:115c::1]:8080"),
    ],
)
def test_origin_of_normalises(url: str, origin: str) -> None:
    assert origin_of(url) == origin


@pytest.mark.parametrize(
    "url",
    ["ftp://push.test/x", "http://user:pw@push.test/x", "http:///nohost", "http://push.test:99999"],
)
def test_origin_of_rejects(url: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - each case has its own message
        origin_of(url)


@pytest.mark.parametrize(
    ("endpoint", "allowed"),
    [
        ("http://push.test/upAbC?up=1", True),
        ("http://push.test:80/upAbC", True),
        ("http://push.test:8080/upAbC", False),
        ("https://push.test/upAbC", False),
        ("http://push.test.evil/upAbC", False),
        ("http://push.test@evil.test/upAbC", False),
        ("http://127.0.0.1:5432/", False),
        ("http://push.test/" + "a" * 3000, False),
        ("not a url", False),
    ],
)
def test_endpoint_allowed(endpoint: str, allowed: bool) -> None:
    assert endpoint_allowed(endpoint, ["http://push.test"]) is allowed


def test_settings_parse_allowed_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TITAN_PUSH_ALLOWED_ORIGINS", "HTTP://100.64.0.1:8080/, https://n.ts.net")
    settings = Settings(database_url=URL)
    assert settings.push_allowed_origins == ("http://100.64.0.1:8080", "https://n.ts.net")
    monkeypatch.setenv("TITAN_PUSH_ALLOWED_ORIGINS", "")
    assert Settings(database_url=URL).push_allowed_origins == ()
    monkeypatch.setenv("TITAN_PUSH_ALLOWED_ORIGINS", "ftp://push.test")
    with pytest.raises(ValidationError):
        Settings(database_url=URL)


def sender(handler: httpx.MockTransport) -> UnifiedPushSender:
    return UnifiedPushSender(httpx.AsyncClient(transport=handler, follow_redirects=False))


async def test_delivers_the_body_as_is() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201)

    body = json.dumps({"notification_id": "x", "kind": "system"}).encode()
    result = await sender(httpx.MockTransport(handle)).send("http://push.test/upAbC?up=1", body)
    assert result is PushResult.DELIVERED
    assert seen[0].method == "POST"
    assert seen[0].content == body
    assert seen[0].headers["TTL"] == "86400"


@pytest.mark.parametrize(
    ("status", "result"),
    [
        (404, PushResult.GONE),
        (410, PushResult.GONE),
        (429, PushResult.FAILED),
        (500, PushResult.FAILED),
        (302, PushResult.FAILED),
    ],
)
async def test_maps_status_codes(status: int, result: PushResult) -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"Location": "http://127.0.0.1:5432/"})

    assert await sender(httpx.MockTransport(handle)).send("http://push.test/up", b"{}") is result


async def test_network_errors_are_failures(caplog: pytest.LogCaptureFixture) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    endpoint = "http://push.test/upSecretTopic123"
    assert await sender(httpx.MockTransport(handle)).send(endpoint, b"{}") is PushResult.FAILED
    assert "upSecretTopic123" not in caplog.text


async def test_client_does_not_follow_redirects() -> None:
    client = new_client(5.0)
    try:
        assert client.follow_redirects is False
    finally:
        await client.aclose()
