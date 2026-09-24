"""Request-scoped dependencies: database session and the authenticated caller."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from titan.agent.runtime import ChatRuntime
from titan.domains.accounts.service import AccountsService, Principal
from titan.domains.chat.service import ChatService
from titan.domains.notifications.service import NotificationsService

# auto_error=False: a missing header is answered by require_principal with a
# problem-details 401 instead of FastAPI's default body.
_bearer = HTTPBearer(auto_error=False, description="Device token from POST /devices/pair")


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessions() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


def get_accounts(session: Session) -> AccountsService:
    return AccountsService(session)


Accounts = Annotated[AccountsService, Depends(get_accounts)]


async def require_principal(
    accounts: Accounts,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    principal = await accounts.resolve_token(credentials.credentials) if credentials else None
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail="missing, unknown or revoked device token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


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
