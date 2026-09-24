"""Account management, pairing and token authentication.

The service owns its transactions: a failed login must be recorded even though
the call then raises, so commits cannot be left to the caller.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from titan.domains.accounts import credentials
from titan.domains.accounts.errors import (
    ForbiddenError,
    InvalidCredentialsError,
    InvalidUsernameError,
    NotFoundError,
    UsernameTakenError,
)
from titan.domains.accounts.models import Device, Platform, Role, User

MAX_FAILED_LOGINS = 10
LOCKOUT = timedelta(minutes=15)
# Coarse on purpose: every write replicates to all nodes (ADR 0006).
LAST_SEEN_RESOLUTION = timedelta(hours=1)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: a user acting through one paired device."""

    user_id: uuid.UUID
    username: str
    role: Role
    device_id: uuid.UUID

    @property
    def is_owner(self) -> bool:
        return self.role is Role.OWNER


@dataclass(frozen=True)
class PairedDevice:
    device: Device
    user: User
    token: str  # shown once, never stored


def _now() -> datetime:
    return datetime.now(UTC)


class AccountsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ users

    async def create_user(
        self,
        username: str,
        password: str,
        *,
        role: Role = Role.MEMBER,
        display_name: str | None = None,
        actor: Principal | None = None,
    ) -> User:
        """Create an account. Only the owner may do this through the API.

        `actor` is None only for the CLI, which runs with shell access to a node.
        """
        if actor is not None and not actor.is_owner:
            raise ForbiddenError("only the owner can create accounts")
        name = credentials.normalize_username(username)
        credentials.check_password(password)
        if await self._user_by_username(name) is not None:
            raise UsernameTakenError(name)
        user = User(
            username=name,
            display_name=(display_name or name).strip()[:64],
            role=role,
            password_hash=await credentials.hash_password(password),
        )
        self.session.add(user)
        await self.session.commit()
        return user

    async def list_users(self, actor: Principal | None) -> list[User]:
        if actor is not None and not actor.is_owner:
            raise ForbiddenError("only the owner can list accounts")
        return list((await self.session.scalars(select(User).order_by(User.username))).all())

    # ---------------------------------------------------------------- pairing

    async def authenticate(self, username: str, password: str) -> User:
        """Check a password, counting failures and locking the account after too many."""
        try:
            name: str | None = credentials.normalize_username(username)
        except InvalidUsernameError:
            name = None  # a malformed name gets the same answer as an unknown one
        user = await self._user_by_username(name)
        now = _now()
        usable = (
            user is not None
            and user.disabled_at is None
            and (user.locked_until is None or user.locked_until <= now)
        )
        # Always one Argon2 verification, so unknown, locked and wrong look alike.
        ok = await credentials.verify_password(
            user.password_hash if user is not None and usable else None, password
        )
        if user is None or not usable:
            raise InvalidCredentialsError()
        if not ok:
            user.failed_logins += 1
            if user.failed_logins >= MAX_FAILED_LOGINS:
                user.failed_logins = 0
                user.locked_until = now + LOCKOUT
            await self.session.commit()
            raise InvalidCredentialsError()
        changed = False
        if user.failed_logins or user.locked_until:
            user.failed_logins, user.locked_until = 0, None
            changed = True
        if credentials.needs_rehash(user.password_hash):
            user.password_hash = await credentials.hash_password(password)
            changed = True
        if changed:
            await self.session.commit()
        return user

    async def pair_device(
        self, username: str, password: str, *, name: str, platform: Platform
    ) -> PairedDevice:
        user = await self.authenticate(username, password)
        token = credentials.new_device_token()
        device = Device(
            user_id=user.id,
            name=name.strip()[:64] or "device",
            platform=platform,
            token_hash=credentials.token_digest(token),
            last_seen_at=_now(),
        )
        self.session.add(device)
        await self.session.commit()
        return PairedDevice(device=device, user=user, token=token)

    async def resolve_token(self, token: str) -> Principal | None:
        """Return the caller behind a device token, or None if it is not valid."""
        if not token.startswith(credentials.TOKEN_PREFIX):
            return None
        row = (
            await self.session.execute(
                select(Device, User)
                .join(User, Device.user_id == User.id)
                .where(Device.token_hash == credentials.token_digest(token))
            )
        ).first()
        if row is None:
            return None
        device, user = row
        if device.revoked_at is not None or user.disabled_at is not None:
            return None
        now = _now()
        if device.last_seen_at is None or now - device.last_seen_at >= LAST_SEEN_RESOLUTION:
            device.last_seen_at = now
            await self.session.commit()
        return Principal(
            user_id=user.id, username=user.username, role=user.role, device_id=device.id
        )

    # ---------------------------------------------------------------- devices

    async def list_devices(self, actor: Principal) -> list[Device]:
        query = select(Device).where(Device.user_id == actor.user_id)
        return list((await self.session.scalars(query.order_by(Device.created_at))).all())

    async def revoke_device(self, actor: Principal, device_id: uuid.UUID) -> Device:
        device = await self.session.get(Device, device_id)
        # Another member's device is reported as missing, not forbidden, so ids
        # of other people's devices cannot be confirmed.
        if device is None or (device.user_id != actor.user_id and not actor.is_owner):
            raise NotFoundError("device not found")
        if device.revoked_at is None:
            device.revoked_at = _now()
            await self.session.commit()
        return device

    async def get_user(self, user_id: uuid.UUID) -> User:
        user = await self.session.get(User, user_id)
        if user is None:
            raise NotFoundError("user not found")
        return user

    async def get_user_by_username(self, username: str) -> User:
        user = await self._user_by_username(credentials.normalize_username(username))
        if user is None:
            raise NotFoundError("user not found")
        return user

    # ---------------------------------------------------------------- helpers

    async def _user_by_username(self, username: str | None) -> User | None:
        if username is None:
            return None
        user: User | None = await self.session.scalar(select(User).where(User.username == username))
        return user
