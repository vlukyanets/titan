"""Push registration and the notification history."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field

from titan.api.deps import CurrentPrincipal, Notifications
from titan.api.problems import PROBLEM_JSON
from titan.domains.notifications.models import NotificationKind
from titan.domains.notifications.service import DEFAULT_PAGE, MAX_PAGE
from titan.notify.unifiedpush import MAX_ENDPOINT_LENGTH

router = APIRouter(tags=["notifications"])

_PROBLEM: dict[str, Any] = {"content": {PROBLEM_JSON: {}}}


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: NotificationKind
    title: str
    body: str
    data: dict[str, Any] = Field(description="Ids the notification's actions refer to")
    created_at: datetime
    delivered_at: datetime | None = Field(
        description="When a push server first accepted it; null if no push got through"
    )
    read_at: datetime | None


class PushRegistration(BaseModel):
    endpoint: str = Field(
        max_length=MAX_ENDPOINT_LENGTH,
        description="The UnifiedPush endpoint the distributor gave the app",
        examples=["http://100.64.0.1:8080/upAbCdEf123456?up=1"],
    )


class ReadAllResult(BaseModel):
    updated: int


@router.put(
    "/devices/current/push",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Register the calling device's UnifiedPush endpoint",
    description=(
        "Replaces any earlier endpoint of this device. The endpoint must be on a push "
        "server the backend is configured to use. Push messages carry only "
        '`{"notification_id", "kind"}`; fetch the notification to show it.'
    ),
    responses={401: _PROBLEM, 422: _PROBLEM},
)
async def register_push(
    body: PushRegistration, principal: CurrentPrincipal, notifications: Notifications
) -> Response:
    await notifications.register_push(principal, body.endpoint)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/devices/current/push",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Stop pushes to the calling device",
    responses={401: _PROBLEM},
)
async def unregister_push(principal: CurrentPrincipal, notifications: Notifications) -> Response:
    await notifications.unregister_push(principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/notifications",
    summary="The caller's notifications, newest first",
    description="Page with `before`: pass the id of the last notification you have.",
    responses={401: _PROBLEM},
)
async def list_notifications(
    principal: CurrentPrincipal,
    notifications: Notifications,
    unread: Annotated[bool, Query(description="Only unread notifications")] = False,
    before: Annotated[uuid.UUID | None, Query(description="Only older than this id")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> list[NotificationOut]:
    items = await notifications.history(principal, unread_only=unread, before=before, limit=limit)
    return [NotificationOut.model_validate(n) for n in items]


@router.post(
    "/notifications/read-all",
    summary="Mark all of the caller's notifications as read",
    responses={401: _PROBLEM},
)
async def mark_all_read(principal: CurrentPrincipal, notifications: Notifications) -> ReadAllResult:
    return ReadAllResult(updated=await notifications.mark_all_read(principal))


@router.post(
    "/notifications/test",
    status_code=status.HTTP_201_CREATED,
    summary="Send a test notification to yourself",
    description="For the push setup screen: `delivered_at` shows whether a push got through.",
    responses={401: _PROBLEM},
)
async def send_test(principal: CurrentPrincipal, notifications: Notifications) -> NotificationOut:
    return NotificationOut.model_validate(await notifications.send_test(principal))


@router.get(
    "/notifications/{notification_id}",
    summary="One notification",
    responses={401: _PROBLEM, 404: _PROBLEM},
)
async def get_notification(
    notification_id: uuid.UUID, principal: CurrentPrincipal, notifications: Notifications
) -> NotificationOut:
    return NotificationOut.model_validate(await notifications.get(principal, notification_id))


@router.post(
    "/notifications/{notification_id}/read",
    summary="Mark a notification as read",
    responses={401: _PROBLEM, 404: _PROBLEM},
)
async def mark_read(
    notification_id: uuid.UUID, principal: CurrentPrincipal, notifications: Notifications
) -> NotificationOut:
    return NotificationOut.model_validate(await notifications.mark_read(principal, notification_id))
