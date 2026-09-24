"""UnifiedPush delivery: an HTTP POST of the message body to the device's endpoint.

The push server (ntfy on the tailnet) forwards the body to the distributor app on
the phone. Endpoints embed a secret topic, so they are never logged.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Iterable
from typing import Protocol
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

MAX_ENDPOINT_LENGTH = 2048
_DEFAULT_PORTS = {"http": 80, "https": 443}


class PushResult(enum.StrEnum):
    DELIVERED = "delivered"
    # The push server no longer knows the endpoint; the subscription should go.
    GONE = "gone"
    FAILED = "failed"


class Pusher(Protocol):
    async def send(self, endpoint: str, body: bytes) -> PushResult: ...


def origin_of(url: str) -> str:
    """Normalise a URL to its origin, e.g. ``HTTP://Host:80/x`` -> ``http://host``.

    Raises ValueError for anything that is not a plain http(s) URL with a host.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError("only http and https URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs with credentials are not allowed")
    host = parts.hostname
    if not host:
        raise ValueError("the URL has no host")
    port = parts.port  # raises ValueError for an out-of-range port
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def endpoint_allowed(endpoint: str, allowed_origins: Iterable[str]) -> bool:
    if len(endpoint) > MAX_ENDPOINT_LENGTH:
        return False
    try:
        origin = origin_of(endpoint)
    except ValueError:
        return False
    return origin in set(allowed_origins)


class UnifiedPushSender:
    """Sends push messages over a shared client; see `new_client` for its settings."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def send(self, endpoint: str, body: bytes) -> PushResult:
        try:
            response = await self.client.post(
                endpoint,
                content=body,
                # Web Push headers: keep undelivered messages for a day, deliver promptly.
                headers={"Content-Type": "application/json", "TTL": "86400", "Urgency": "high"},
            )
        except httpx.HTTPError as exc:
            log.warning("push failed: %s", type(exc).__name__)
            return PushResult.FAILED
        if response.status_code in (404, 410):
            return PushResult.GONE
        if response.is_success:
            return PushResult.DELIVERED
        log.warning("push rejected with HTTP %d", response.status_code)
        return PushResult.FAILED


def new_client(timeout: float) -> httpx.AsyncClient:
    # No redirects: an allowed push server must not be able to bounce requests to
    # other addresses. No proxy from the environment: pushes stay on the tailnet.
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False)
