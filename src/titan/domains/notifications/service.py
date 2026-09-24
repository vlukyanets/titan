"""Storing notifications, pushing them to devices, and the notification history.

The service owns its transactions: a notification is committed before it is
pushed, so a crash mid-push never loses it, and the history is the fallback for
every push that does not arrive.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.accounts.models import Device, User
from titan.domains.accounts.service import Principal
from titan.domains.notifications.errors import InvalidPushEndpointError, NotFoundError
from titan.domains.notifications.models import Notification, NotificationKind, PushSubscription
from titan.notify import Pusher, PushResult, endpoint_allowed
from titan.storage.ids import uuid7

DEFAULT_PAGE = 50
MAX_PAGE = 100
TITLE_LENGTH = 120
BODY_LENGTH = 1000


def _now() -> datetime:
    return datetime.now(UTC)


def push_message(notification: Notification) -> bytes:
    """The whole push payload: a reference, never content (ADR 0008)."""
    return json.dumps(
        {"notification_id": str(notification.id), "kind": notification.kind.value},
        separators=(",", ":"),
    ).encode()


class NotificationsService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        pusher: Pusher | None = None,
        push_origins: Iterable[str] = (),
    ) -> None:
        self.session = session
        self.pusher = pusher
        self.push_origins = tuple(push_origins)

    # ------------------------------------------------------------- sending

    async def notify(
        self,
        user_id: uuid.UUID,
        kind: NotificationKind,
        title: str,
        body: str = "",
        data: dict[str, Any] | None = None,
        *,
        notification_id: uuid.UUID | None = None,
    ) -> Notification:
        """Store a notification for a user, then push it to their devices.

        Scheduled jobs pass a deterministic `notification_id`, so a job that ran
        on two nodes produces one notification once they replicate (ADR 0006).
        """
        notification = Notification(
            id=notification_id or uuid7(),
            user_id=user_id,
            kind=kind,
            title=title.strip()[:TITLE_LENGTH] or kind.value,
            body=body.strip()[:BODY_LENGTH],
            data=data or {},
        )
        self.session.add(notification)
        await self.session.commit()
        await self.deliver(notification)
        return notification

    async def send_test(self, actor: Principal) -> Notification:
        return await self.notify(
            actor.user_id,
            NotificationKind.SYSTEM,
            "Test notification",
            "Push notifications reach this device.",
        )

    async def deliver(self, notification: Notification) -> int:
        """Push to every active device of the user. Returns how many accepted it."""
        if self.pusher is None:
            return 0
        pusher = self.pusher
        subscriptions = list(
            (
                await self.session.scalars(
                    select(PushSubscription)
                    .join(Device, Device.id == PushSubscription.device_id)
                    .join(User, User.id == Device.user_id)
                    .where(
                        Device.user_id == notification.user_id,
                        Device.revoked_at.is_(None),
                        User.disabled_at.is_(None),
                    )
                )
            ).all()
        )
        if not subscriptions:
            return 0
        message = push_message(notification)
        results = await asyncio.gather(*(pusher.send(s.endpoint, message) for s in subscriptions))
        for subscription, result in zip(subscriptions, results, strict=True):
            if result is PushResult.GONE:
                await self.session.delete(subscription)
        delivered = sum(result is PushResult.DELIVERED for result in results)
        if delivered and notification.delivered_at is None:
            notification.delivered_at = _now()
        if delivered or PushResult.GONE in results:
            await self.session.commit()
        return delivered

    # ------------------------------------------------------------- history

    async def history(
        self,
        actor: Principal,
        *,
        unread_only: bool = False,
        before: uuid.UUID | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> list[Notification]:
        query = select(Notification).where(Notification.user_id == actor.user_id)
        if unread_only:
            query = query.where(Notification.read_at.is_(None))
        if before is not None:
            query = query.where(Notification.id < before)
        query = query.order_by(Notification.id.desc()).limit(max(1, min(limit, MAX_PAGE)))
        return list((await self.session.scalars(query)).all())

    async def get(self, actor: Principal, notification_id: uuid.UUID) -> Notification:
        notification = await self.session.get(Notification, notification_id)
        # Someone else's notification is reported as missing, so ids cannot be probed.
        if notification is None or notification.user_id != actor.user_id:
            raise NotFoundError("notification not found")
        return notification

    async def mark_read(self, actor: Principal, notification_id: uuid.UUID) -> Notification:
        notification = await self.get(actor, notification_id)
        if notification.read_at is None:
            notification.read_at = _now()
            await self.session.commit()
        return notification

    async def mark_all_read(self, actor: Principal) -> int:
        result: CursorResult[Any] = await self.session.execute(  # type: ignore[assignment]
            update(Notification)
            .where(Notification.user_id == actor.user_id, Notification.read_at.is_(None))
            .values(read_at=_now())
        )
        await self.session.commit()
        return result.rowcount

    # ---------------------------------------------------------- subscriptions

    async def register_push(self, actor: Principal, endpoint: str) -> None:
        """Register or replace the calling device's UnifiedPush endpoint."""
        endpoint = endpoint.strip()
        if not self.push_origins:
            raise InvalidPushEndpointError(
                "this server accepts no push endpoints: no push servers are configured"
            )
        if not endpoint_allowed(endpoint, self.push_origins):
            raise InvalidPushEndpointError(
                "the endpoint is not on a push server this server is configured to use"
            )
        now = _now()
        statement = insert(PushSubscription).values(
            device_id=actor.device_id, endpoint=endpoint, created_at=now, updated_at=now
        )
        # One subscription per device; concurrent registrations must not collide.
        await self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[PushSubscription.device_id],
                set_={"endpoint": statement.excluded.endpoint, "updated_at": now},
                where=PushSubscription.endpoint != statement.excluded.endpoint,
            )
        )
        await self.session.commit()

    async def unregister_push(self, actor: Principal) -> None:
        await self.session.execute(
            delete(PushSubscription).where(PushSubscription.device_id == actor.device_id)
        )
        await self.session.commit()
