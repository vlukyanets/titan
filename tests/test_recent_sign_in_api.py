"""Sensitive changes from a browser need a recent password (ADR 0012)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from tests.test_accounts_api import OWNER_PW, Api
from tests.test_session_api import SAME_SITE, api, web_device
from titan.domains.accounts.models import Device, Role

__all__ = ["api"]  # the fixture

pytestmark = pytest.mark.db

NEW_USER = {"username": "boris", "password": "a-long-member-password"}


async def age_sign_in(api: Api, device_id: object, minutes: int = 20) -> None:
    await api.execute(
        update(Device)
        .where(Device.id == device_id)
        .values(signed_in_at=datetime.now(UTC) - timedelta(minutes=minutes))
    )


async def test_an_old_browser_sign_in_must_confirm_the_password(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    signed_in = await api.client.post(
        "/api/v1/session", json={"username": "anna", "password": OWNER_PW}, headers=SAME_SITE
    )
    assert signed_in.status_code == 201
    device = await web_device(api)
    await age_sign_in(api, device.id)

    refused = await api.client.post("/api/v1/users", json=NEW_USER, headers=SAME_SITE)
    assert refused.status_code == 403
    assert refused.json()["type"] == "urn:titan:problem:confirm-password"
    policy = await api.client.put(
        "/api/v1/policy/tasks/destructive", json={"decision": "deny"}, headers=SAME_SITE
    )
    assert policy.status_code == 403
    # Everyday changes need no confirmation.
    note = await api.client.post("/api/v1/notes", json={"title": "Milk"}, headers=SAME_SITE)
    assert note.status_code == 201

    wrong = await api.client.post(
        "/api/v1/session/confirm", json={"password": "a-wrong-password-1"}, headers=SAME_SITE
    )
    assert wrong.status_code == 401
    confirmed = await api.client.post(
        "/api/v1/session/confirm", json={"password": OWNER_PW}, headers=SAME_SITE
    )
    assert confirmed.status_code == 204
    created = await api.client.post("/api/v1/users", json=NEW_USER, headers=SAME_SITE)
    assert created.status_code == 201, created.text
    assert (await web_device(api)).id == device.id  # no new device


async def test_paired_devices_are_not_asked_to_confirm(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    paired = await api.pair("anna", OWNER_PW)
    await age_sign_in(api, paired.json()["device_id"], minutes=60 * 24)
    bearer = {"Authorization": f"Bearer {paired.json()['token']}"}
    created = await api.client.post("/api/v1/users", json=NEW_USER, headers=bearer)
    assert created.status_code == 201


async def test_revoking_another_users_device_needs_a_recent_sign_in(api: Api) -> None:
    await api.create_user("anna", OWNER_PW, Role.OWNER)
    await api.create_user("boris", NEW_USER["password"])
    phone = (await api.pair("boris", NEW_USER["password"])).json()["device_id"]
    await api.client.post(
        "/api/v1/session", json={"username": "anna", "password": OWNER_PW}, headers=SAME_SITE
    )
    browser = await web_device(api)
    await age_sign_in(api, browser.id)
    other = await api.client.delete(f"/api/v1/devices/{phone}", headers=SAME_SITE)
    assert other.status_code == 403
    own = await api.client.delete(f"/api/v1/devices/{browser.id}", headers=SAME_SITE)
    assert own.status_code == 204
