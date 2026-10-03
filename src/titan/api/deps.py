"""Request-scoped dependencies: database session and the authenticated caller."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from titan.agent.runtime import ChatRuntime
from titan.domains.accounts.models import Platform
from titan.domains.accounts.service import WEB_IDLE, AccountsService, Principal
from titan.domains.chat.service import ChatService
from titan.domains.notifications.service import NotificationsService

# auto_error=False: a missing header is answered by require_principal with a
# problem-details 401 instead of FastAPI's default body.
_bearer = HTTPBearer(auto_error=False, description="Device token from POST /devices/pair")

# Browser sessions (ADR 0012). `__Host-` ties the cookie to the exact address.
SESSION_COOKIE = "__Host-TSID"
_cookie = APIKeyCookie(
    name=SESSION_COOKIE, auto_error=False, description="Set by POST /session; browsers only"
)
REQUEST_HEADER = "X-Titan-Request"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(WEB_IDLE.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="strict",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")


def _cleared_cookie() -> str:
    response = Response()
    clear_session_cookie(response)
    return response.headers["set-cookie"]


def check_same_site(request: Request) -> None:
    """Refuse what another site could make a browser send (ADR 0012)."""
    origin = request.headers.get("origin")
    if request.headers.get(REQUEST_HEADER) != "1" or (
        origin is not None and urlsplit(origin).netloc != request.headers.get("host")
    ):
        raise HTTPException(status_code=403, detail=f"cross-site request: send {REQUEST_HEADER}: 1")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessions() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


def get_accounts(session: Session) -> AccountsService:
    return AccountsService(session)


Accounts = Annotated[AccountsService, Depends(get_accounts)]


async def require_principal(
    request: Request,
    response: Response,
    accounts: Accounts,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    cookie: Annotated[str | None, Depends(_cookie)],
) -> Principal:
    """The bearer header first, then a browser's session cookie."""
    if credentials:
        resolved = await accounts.resolve(credentials.credentials)
    elif cookie:
        resolved = await accounts.resolve(cookie, platform=Platform.WEB)
    else:
        resolved = None
    if resolved is None:
        headers = {"WWW-Authenticate": "Bearer"}
        if cookie and not credentials:
            headers["Set-Cookie"] = _cleared_cookie()
        raise HTTPException(
            status_code=401, detail="missing, unknown or revoked device token", headers=headers
        )
    if cookie and not credentials:
        if request.method not in SAFE_METHODS:
            check_same_site(request)
        if resolved.seen:
            # ponytail: lost on routes that return their own Response (SSE, 204);
            # the next renewal is an hour later, well within the 30 days.
            set_session_cookie(response, cookie)
    return resolved.principal


CurrentPrincipal = Annotated[Principal, Depends(require_principal)]


def get_notifications(request: Request, session: Session) -> NotificationsService:
    return NotificationsService(
        session,
        pusher=request.app.state.pusher,
        push_origins=request.app.state.settings.push_allowed_origins,
    )


Notifications = Annotated[NotificationsService, Depends(get_notifications)]


def get_chat(request: Request, session: Session) -> ChatService:
    timeout = request.app.state.settings.chat_turn_timeout_seconds
    return ChatService(session, turn_timeout=timedelta(seconds=timeout))


Chat = Annotated[ChatService, Depends(get_chat)]


def get_chat_turns(request: Request) -> ChatRuntime:
    runtime: ChatRuntime = request.app.state.chat_runtime
    return runtime


ChatTurns = Annotated[ChatRuntime, Depends(get_chat_turns)]
