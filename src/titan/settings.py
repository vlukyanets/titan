"""Runtime configuration, read from TITAN_* environment variables."""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
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
    # A titan-web build (index.html and assets/) that titan-api serves next to the
    # API (titan-web ADR 0002). The image points it at its pinned release; unset,
    # the node serves only the API.
    web_ui_dir: Path | None = None

    # Claude credentials (ADR 0003, docs/architecture/claude-auth.md).
    claude_auth_mode: Literal["api-key", "oauth"] = "api-key"
    # Dedicated Claude Code configuration, so no user or project settings apply.
    claude_config_dir: Path = Path.home() / ".local" / "state" / "titan" / "claude"
    claude_model_fast: str = "claude-haiku-4-5"
    claude_model_strong: str = "claude-sonnet-5"

    # Chat (docs/spec/domains/chat.md).
    chat_history_messages: int = Field(default=40, ge=0, le=200)
    chat_turn_timeout_seconds: float = Field(default=300.0, gt=0)
    # Autonomy (docs/spec/domains/autonomy.md): unanswered approvals expire.
    approval_ttl_hours: float = Field(default=24.0, gt=0)

    # Scheduler (docs/architecture/overview.md#scheduler-and-reminders).
    node_name: str = Field(default_factory=socket.gethostname, min_length=1, max_length=64)
    # The node that runs sweeps while it is up; empty means this node.
    preferred_node: str | None = Field(default=None, max_length=64)
    scheduler_tick_seconds: float = Field(default=5.0, gt=0, le=60)
    scheduler_lease_seconds: float = Field(default=30.0, gt=0)
    scheduler_grace_seconds: float = Field(default=30.0, ge=0)
    # Touched after every successful tick, for the container's health check.
    worker_heartbeat_file: Path | None = None

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
