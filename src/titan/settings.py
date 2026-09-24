"""Runtime configuration, read from TITAN_* environment variables."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Annotated, Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from titan.notify import origin_of


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
    # Push servers that device endpoints may point at, as comma-separated origins
    # such as http://100.64.0.1:8080 (ADR 0008). Empty refuses push registration.
    push_allowed_origins: Annotated[tuple[str, ...], NoDecode] = ()
    push_timeout_seconds: float = 5.0

    # Claude credentials (ADR 0003, docs/architecture/claude-auth.md).
    claude_auth_mode: Literal["api-key", "oauth"] = "api-key"
    # Dedicated Claude Code configuration, so no user or project settings apply.
    claude_config_dir: Path = Path.home() / ".local" / "state" / "titan" / "claude"
    claude_model_fast: str = "claude-haiku-4-5"
    claude_model_strong: str = "claude-sonnet-5"

    @field_validator("bind_host")
    @classmethod
    def _valid_host(cls, value: str) -> str:
        ipaddress.ip_address(value)
        return value

    @field_validator("push_allowed_origins", mode="before")
    @classmethod
    def _origins(cls, value: object) -> object:
        if isinstance(value, str):
            value = [item for item in value.split(",") if item.strip()]
        if isinstance(value, list | tuple):
            return tuple(origin_of(str(item)) for item in value)
        return value

    def check_bind(self) -> None:
        if ipaddress.ip_address(self.bind_host).is_unspecified and not self.allow_wildcard_bind:
            raise ValueError(
                f"refusing to listen on {self.bind_host}: bind to this node's Tailscale address "
                "or set TITAN_ALLOW_WILDCARD_BIND=true inside a container"
            )
