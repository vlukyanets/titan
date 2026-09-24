"""Pairing, the current user, devices and owner-only account management."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field

from titan.api.deps import Accounts, CurrentPrincipal
from titan.api.problems import PROBLEM_JSON
from titan.domains.accounts.models import Device, Platform, Role, User

router = APIRouter(tags=["accounts"])

_PROBLEM: dict[str, Any] = {"content": {PROBLEM_JSON: {}}}


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    display_name: str
    role: Role
    created_at: datetime


class DeviceOut(BaseModel):
    id: uuid.UUID
    name: str
    platform: Platform
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None
    current: bool = Field(description="True for the device making this request")

    @classmethod
    def of(cls, device: Device, current_device: uuid.UUID) -> DeviceOut:
        return cls(
            id=device.id,
            name=device.name,
            platform=device.platform,
            created_at=device.created_at,
            last_seen_at=device.last_seen_at,
            revoked_at=device.revoked_at,
            current=device.id == current_device,
        )


class PairRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)
    device_name: str = Field(min_length=1, max_length=64, examples=["Pixel 9"])
    platform: Platform


class PairResponse(BaseModel):
    device_id: uuid.UUID
    token: str = Field(description="Shown once. Send as 'Authorization: Bearer <token>'.")
    user: UserOut


class CreateUserRequest(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)
    display_name: str | None = Field(default=None, max_length=64)


@router.post(
    "/devices/pair",
    status_code=status.HTTP_201_CREATED,
    summary="Pair a device with username and password",
    responses={401: _PROBLEM | {"description": "Invalid credentials or locked account"}},
)
async def pair_device(body: PairRequest, accounts: Accounts) -> PairResponse:
    paired = await accounts.pair_device(
        body.username, body.password, name=body.device_name, platform=body.platform
    )
    return PairResponse(
        device_id=paired.device.id, token=paired.token, user=UserOut.model_validate(paired.user)
    )


@router.get("/me", summary="The authenticated user", responses={401: _PROBLEM})
async def me(principal: CurrentPrincipal, accounts: Accounts) -> UserOut:
    return UserOut.model_validate(await accounts.get_user(principal.user_id))


@router.get("/devices", summary="The caller's paired devices", responses={401: _PROBLEM})
async def list_devices(principal: CurrentPrincipal, accounts: Accounts) -> list[DeviceOut]:
    devices = await accounts.list_devices(principal)
    return [DeviceOut.of(d, principal.device_id) for d in devices]


@router.delete(
    "/devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a device",
    description="Users revoke their own devices; the owner can revoke any device.",
    responses={401: _PROBLEM, 404: _PROBLEM},
)
async def revoke_device(
    device_id: uuid.UUID, principal: CurrentPrincipal, accounts: Accounts
) -> Response:
    await accounts.revoke_device(principal, device_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users", summary="All accounts (owner only)", responses={401: _PROBLEM, 403: _PROBLEM})
async def list_users(principal: CurrentPrincipal, accounts: Accounts) -> list[UserOut]:
    return [UserOut.model_validate(u) for u in await accounts.list_users(principal)]


@router.post(
    "/users",
    status_code=status.HTTP_201_CREATED,
    summary="Create a member account (owner only)",
    responses={401: _PROBLEM, 403: _PROBLEM, 409: _PROBLEM},
)
async def create_user(
    body: CreateUserRequest, principal: CurrentPrincipal, accounts: Accounts
) -> UserOut:
    user: User = await accounts.create_user(
        body.username, body.password, display_name=body.display_name, actor=principal
    )
    return UserOut.model_validate(user)
