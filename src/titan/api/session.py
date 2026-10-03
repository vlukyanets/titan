"""Browser sign-in and sign-out with a session cookie (ADR 0012)."""

from __future__ import annotations

import time
import uuid
from collections import deque
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from titan.api.accounts import UserOut
from titan.api.deps import (
    Accounts,
    CurrentPrincipal,
    Notifications,
    check_same_site,
    clear_session_cookie,
    set_session_cookie,
)
from titan.api.problems import PROBLEM_JSON
from titan.domains.notifications.models import NotificationKind

router = APIRouter(tags=["accounts"])

_PROBLEM: dict[str, Any] = {"content": {PROBLEM_JSON: {}}}

# Sign-in attempts per client address and minute.
SIGN_IN_LIMIT = 10
SIGN_IN_WINDOW = 60.0

_BROWSERS = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
)
_SYSTEMS = (
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iOS"),
    ("CrOS", "ChromeOS"),
    ("Windows", "Windows"),
    ("Mac OS X", "macOS"),
    ("Linux", "Linux"),
)


def browser_name(user_agent: str | None) -> str:
    """'Firefox on Linux' from a User-Agent header, or 'Browser'."""
    agent = user_agent or ""
    browser = next((name for mark, name in _BROWSERS if mark in agent), "Browser")
    system = next((name for mark, name in _SYSTEMS if mark in agent), None)
    return f"{browser} on {system}" if system else browser


class SignInLimiter:
    """Recent sign-in attempts per address, in this node's memory."""

    def __init__(self, limit: int = SIGN_IN_LIMIT, window: float = SIGN_IN_WINDOW) -> None:
        self.limit = limit
        self.window = window
        self._attempts: dict[str, deque[float]] = {}

    def allow(self, address: str) -> bool:
        now = time.monotonic()
        # Forget addresses that went quiet, so the map stays small.
        for key in [k for k, v in self._attempts.items() if v[-1] <= now - self.window]:
            del self._attempts[key]
        attempts = self._attempts.setdefault(address, deque())
        while attempts and attempts[0] <= now - self.window:
            attempts.popleft()
        if len(attempts) >= self.limit:
            return False
        attempts.append(now)
        return True


class SignInRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)


class SessionOut(BaseModel):
    device_id: uuid.UUID
    user: UserOut


@router.post(
    "/session",
    status_code=status.HTTP_201_CREATED,
    summary="Sign a browser in",
    description=(
        "Creates a `web` device and sets its token in the `__Host-TSID` cookie; the token "
        "is never in the body. Needs the `X-Titan-Request: 1` header."
    ),
    responses={
        401: _PROBLEM | {"description": "Invalid credentials or locked account"},
        403: _PROBLEM | {"description": "Cross-site request"},
        429: _PROBLEM | {"description": "Too many sign-in attempts from this address"},
    },
)
async def sign_in(
    body: SignInRequest,
    request: Request,
    response: Response,
    accounts: Accounts,
    notifications: Notifications,
) -> SessionOut:
    check_same_site(request)
    limiter: SignInLimiter = request.app.state.sign_in_limiter
    if not limiter.allow(request.client.host if request.client else "unknown"):
        raise HTTPException(status_code=429, detail="too many sign-in attempts, try again soon")
    name = browser_name(request.headers.get("user-agent"))
    signed_in = await accounts.sign_in(body.username, body.password, name=name)
    node = request.app.state.settings.node_name
    await notifications.notify(
        signed_in.user.id,
        NotificationKind.SYSTEM,
        "New sign-in",
        f"{name} signed in on {node}.",
        {"device_id": str(signed_in.device.id)},
    )
    set_session_cookie(response, signed_in.token)
    return SessionOut(device_id=signed_in.device.id, user=UserOut.model_validate(signed_in.user))


@router.delete(
    "/session",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out: revoke the calling device and clear the cookie",
    responses={401: _PROBLEM, 403: _PROBLEM},
)
async def sign_out(principal: CurrentPrincipal, accounts: Accounts) -> Response:
    await accounts.revoke_device(principal, principal.device_id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_session_cookie(response)
    return response
