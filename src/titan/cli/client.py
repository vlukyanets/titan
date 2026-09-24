"""The client side of `titan`: a saved login and calls to a node's HTTP API.

Client commands never touch a database; they pair as a `cli` device and send
its token, like the Android app (docs/spec/cli.md, ADR 0011).
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Self

import httpx

CONFIG_ENV = "TITAN_CLIENT_CONFIG"
API_PREFIX = "/api/v1"
TIMEOUT = httpx.Timeout(30.0)
# A reply can go quiet for a long time while a tool runs; the node ends the turn
# after its own time limit.
STREAM_TIMEOUT = httpx.Timeout(30.0, read=None)


class ClientError(Exception):
    """A failure to show as is: never contains the token or a password."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotLoggedInError(ClientError):
    def __init__(self) -> None:
        super().__init__("not logged in: run `titan login SERVER USERNAME` first")


def config_path(environ: Mapping[str, str]) -> Path:
    if environ.get(CONFIG_ENV):
        return Path(environ[CONFIG_ENV])
    base = environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "titan" / "client.json"


@dataclass(frozen=True)
class Login:
    """What `titan login` saves. Never the password."""

    server: str
    username: str
    device_id: uuid.UUID
    token: str
    last_thread: uuid.UUID | None = None

    @classmethod
    def load(cls, path: Path) -> Login:
        try:
            raw = json.loads(path.read_text())
            return cls(
                server=str(raw["server"]),
                username=str(raw["username"]),
                device_id=uuid.UUID(raw["device_id"]),
                token=str(raw["token"]),
                last_thread=uuid.UUID(raw["last_thread"]) if raw.get("last_thread") else None,
            )
        except FileNotFoundError:
            raise NotLoggedInError from None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ClientError(
                f"{path} is unreadable ({type(exc).__name__}); log in again"
            ) from None

    def save(self, path: Path) -> None:
        """Write the file readable by its owner only, replacing it atomically."""
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = {
            key: str(value) if value is not None else None for key, value in asdict(self).items()
        }
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(data, file, indent=2)
                file.write("\n")
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            raise

    def with_thread(self, thread_id: uuid.UUID) -> Login:
        return replace(self, last_thread=thread_id)


def normalize_server(server: str) -> str:
    server = server.strip().rstrip("/")
    if not server.startswith(("http://", "https://")):
        raise ClientError(
            f"the server must be a URL such as http://titan-home:8000, not {server!r}"
        )
    return server


def problem_message(response: httpx.Response) -> str:
    """An API error as one line, from its problem details when it has them."""
    title, detail = response.reason_phrase or f"HTTP {response.status_code}", ""
    with contextlib.suppress(ValueError, AttributeError):
        body = response.json()
        title = str(body.get("title") or title)
        detail = body.get("detail") or ""
        if not isinstance(detail, str):  # validation errors carry a list
            detail = "; ".join(str(item.get("msg", item)) for item in detail)
    message = f"{title}: {detail}" if detail else title
    if response.status_code == 401:
        message += " (the device may have been revoked; `titan login` pairs it again)"
    return f"{message}"


@dataclass(frozen=True)
class ServerEvent:
    event: str
    data: str


async def server_events(lines: AsyncIterator[str]) -> AsyncIterator[ServerEvent]:
    """Parse a text/event-stream body into events."""
    event, data = "message", list[str]()
    async for line in lines:
        if not line:
            if data:
                yield ServerEvent(event, "\n".join(data))
            event, data = "message", []
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if field == "event":
                event = value
            elif field == "data":
                data.append(value)
    if data:
        yield ServerEvent(event, "\n".join(data))


class Api:
    """A node's API, with or without a device token."""

    def __init__(
        self,
        server: str,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(
            base_url=normalize_server(server) + API_PREFIX,
            headers=headers,
            transport=transport,
            timeout=TIMEOUT,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def call(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        try:
            response = await self._client.request(
                method, path, json=body, params={k: v for k, v in params.items() if v is not None}
            )
        except httpx.HTTPError as exc:
            raise ClientError(
                f"cannot reach {self._client.base_url} ({type(exc).__name__})"
            ) from None
        if response.is_error:
            raise ClientError(problem_message(response), response.status_code)
        return response.json() if response.content else None

    async def stream(self, path: str, body: Any) -> AsyncIterator[ServerEvent]:
        try:
            async with self._client.stream(
                "POST", path, json=body, timeout=STREAM_TIMEOUT
            ) as response:
                if response.is_error:
                    await response.aread()
                    raise ClientError(problem_message(response), response.status_code)
                async for event in server_events(response.aiter_lines()):
                    yield event
        except httpx.HTTPError as exc:
            raise ClientError(
                f"the connection to {self._client.base_url} broke ({type(exc).__name__}); "
                "the reply is still written on the node"
            ) from None
