"""Runtime configuration, read from TITAN_* environment variables."""

from __future__ import annotations

import ipaddress

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TITAN_", extra="ignore")

    # SQLAlchemy URL, e.g. postgresql+psycopg://user:password@host:5432/titan.
    # Secret so the password never ends up in logs or reprs.
    database_url: SecretStr
    bind_host: str = "127.0.0.1"
    port: int = 8000
    # Inside a container the API must listen on all interfaces of its own network
    # namespace; exposure is then decided by the published port. Everywhere else a
    # wildcard address would be a public listener, which the security rules forbid.
    allow_wildcard_bind: bool = False
    log_level: str = "INFO"

    @field_validator("bind_host")
    @classmethod
    def _valid_host(cls, value: str) -> str:
        ipaddress.ip_address(value)
        return value

    def check_bind(self) -> None:
        if ipaddress.ip_address(self.bind_host).is_unspecified and not self.allow_wildcard_bind:
            raise ValueError(
                f"refusing to listen on {self.bind_host}: bind to this node's Tailscale address "
                "or set TITAN_ALLOW_WILDCARD_BIND=true inside a container"
            )
