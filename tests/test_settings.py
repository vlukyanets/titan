from __future__ import annotations

import pytest
from pydantic import ValidationError

from titan.settings import Settings

URL = "postgresql+psycopg://user:hunter2@db/titan"


def test_database_password_is_not_in_repr() -> None:
    settings = Settings(database_url=URL)
    assert "hunter2" not in repr(settings)
    assert settings.database_url.get_secret_value() == URL


def test_defaults_to_loopback() -> None:
    settings = Settings(database_url=URL)
    assert settings.bind_host == "127.0.0.1"
    settings.check_bind()


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_refuses_wildcard_bind_outside_containers(host: str) -> None:
    settings = Settings(database_url=URL, bind_host=host)
    with pytest.raises(ValueError, match="refusing to listen"):
        settings.check_bind()


def test_wildcard_bind_allowed_when_explicit() -> None:
    settings = Settings(database_url=URL, bind_host="0.0.0.0", allow_wildcard_bind=True)
    settings.check_bind()


def test_tailscale_address_is_accepted() -> None:
    Settings(database_url=URL, bind_host="100.101.102.103").check_bind()


def test_rejects_hostnames() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url=URL, bind_host="example.com")
