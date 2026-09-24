"""Strict Claude credential modes for the Agent SDK (docs/architecture/claude-auth.md).

Claude Code picks the first credential it finds, so a stray API key would quietly
win over an OAuth token (and the other way round for a gateway token). The agent
process therefore keeps exactly one credential in its environment. The SDK merges
its `env` option over `os.environ`, so cleaning has to happen in `os.environ`
itself, before the first SDK call.
"""

from __future__ import annotations

import enum
import logging
import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path

from claude_agent_sdk import SystemMessage

from titan.settings import Settings

log = logging.getLogger(__name__)

AUTH_DOC = "https://github.com/vlukyanets/titan/blob/master/docs/architecture/claude-auth.md"

# Everything Claude Code reads for credentials, providers and settings lives under
# these prefixes (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL,
# CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY, ...). All of
# it is dropped, then the one credential of the selected mode is put back.
_REMOVED_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "CLAUDECODE")
_REMOVED_NAMES = frozenset({"AWS_BEARER_TOKEN_BEDROCK"})


class AuthMode(enum.StrEnum):
    API_KEY = "api-key"
    OAUTH = "oauth"


CREDENTIAL = {AuthMode.API_KEY: "ANTHROPIC_API_KEY", AuthMode.OAUTH: "CLAUDE_CODE_OAUTH_TOKEN"}

# The `apiKeySource` a session must report in its system/init message. Claude Code
# 2.1.281 resolves it to ANTHROPIC_API_KEY, apiKeyHelper, "/login managed key" or
# "none"; an OAuth token is not an API key source, so OAuth sessions report "none".
EXPECTED_API_KEY_SOURCE = {AuthMode.API_KEY: "ANTHROPIC_API_KEY", AuthMode.OAUTH: "none"}


class ClaudeAuthError(RuntimeError):
    """The credential setup does not match the selected mode. Never contains secrets."""


def _removed(name: str) -> bool:
    return name.startswith(_REMOVED_PREFIXES) or name in _REMOVED_NAMES


def subprocess_environment(
    mode: AuthMode, environ: Mapping[str, str], config_dir: Path
) -> dict[str, str]:
    """The environment the Claude Code subprocess may see in `mode`."""
    name = CREDENTIAL[mode]
    credential = environ.get(name, "").strip()
    if not credential:
        raise ClaudeAuthError(f"{name} is not set, but TITAN_CLAUDE_AUTH_MODE is {mode.value}")
    env = {key: value for key, value in environ.items() if not _removed(key)}
    env[name] = credential
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def verify_environment(mode: AuthMode, environ: Mapping[str, str], config_dir: Path) -> None:
    """Refuse to go on if anything but the selected credential is visible."""
    allowed = {CREDENTIAL[mode], "CLAUDE_CONFIG_DIR"}
    stray = sorted(key for key in environ if _removed(key) and key not in allowed)
    if stray:
        raise ClaudeAuthError(f"variables from another credential source are set: {stray}")
    if not environ.get(CREDENTIAL[mode]):
        raise ClaudeAuthError(f"{CREDENTIAL[mode]} is not set")
    if environ.get("CLAUDE_CONFIG_DIR") != str(config_dir):
        raise ClaudeAuthError("CLAUDE_CONFIG_DIR does not point at TITAN's own directory")


def install(settings: Settings, environ: MutableMapping[str, str] | None = None) -> AuthMode:
    """Clean the process environment for the Agent SDK. Call once at agent startup."""
    target = os.environ if environ is None else environ
    mode = AuthMode(settings.claude_auth_mode)
    cleaned = subprocess_environment(mode, target, settings.claude_config_dir)
    settings.claude_config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.clear()
    target.update(cleaned)
    if mode is AuthMode.OAUTH:
        log.warning(
            "Claude oauth mode: a personal subscription token is in use. This is for the "
            "owner's own use at their own risk and may stop working; see %s",
            AUTH_DOC,
        )
    return mode


def check_init(mode: AuthMode, message: SystemMessage) -> str:
    """Check the credential source a session reports before it calls the model."""
    source = message.data.get("apiKeySource")
    expected = EXPECTED_API_KEY_SOURCE[mode]
    if source != expected:
        raise ClaudeAuthError(
            f"Claude Code reports credential source {source!r}, expected {expected!r} "
            f"in {mode.value} mode"
        )
    return expected
