"""Strict credential modes: environment cleaning and the system/init check."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from claude_agent_sdk import SystemMessage

from titan.agent import auth
from titan.agent.auth import AuthMode, ClaudeAuthError
from titan.settings import Settings

URL = "postgresql+psycopg://user:pw@db/titan"
API_KEY = "sk-ant-api-fake-key-for-tests"
OAUTH_TOKEN = "sk-ant-oat01-fake-token-for-tests"

# Every credential and provider switch at once, plus unrelated variables that must stay.
POLLUTED = {
    "ANTHROPIC_API_KEY": API_KEY,
    "CLAUDE_CODE_OAUTH_TOKEN": OAUTH_TOKEN,
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR": "3",
    "ANTHROPIC_AUTH_TOKEN": "fake-gateway-token",
    "ANTHROPIC_BASE_URL": "http://gateway.test",
    "ANTHROPIC_FOUNDRY_API_KEY": "fake-foundry-key",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
    "CLAUDE_CODE_USE_FOUNDRY": "1",
    "AWS_BEARER_TOKEN_BEDROCK": "fake-bedrock-token",
    "CLAUDE_CONFIG_DIR": "/home/someone/.claude",
    "CLAUDECODE": "1",
    "PATH": "/usr/bin",
    "HOME": "/app",
    "TITAN_DATABASE_URL": URL,
}
KEPT = {"PATH": "/usr/bin", "HOME": "/app", "TITAN_DATABASE_URL": URL}


def test_api_key_mode_keeps_only_the_api_key(tmp_path: Path) -> None:
    env = auth.subprocess_environment(AuthMode.API_KEY, POLLUTED, tmp_path)
    assert env == KEPT | {"ANTHROPIC_API_KEY": API_KEY, "CLAUDE_CONFIG_DIR": str(tmp_path)}


def test_oauth_mode_keeps_only_the_oauth_token(tmp_path: Path) -> None:
    env = auth.subprocess_environment(AuthMode.OAUTH, POLLUTED, tmp_path)
    assert env == KEPT | {
        "CLAUDE_CODE_OAUTH_TOKEN": OAUTH_TOKEN,
        "CLAUDE_CONFIG_DIR": str(tmp_path),
    }


@pytest.mark.parametrize(
    ("mode", "missing"),
    [(AuthMode.API_KEY, "ANTHROPIC_API_KEY"), (AuthMode.OAUTH, "CLAUDE_CODE_OAUTH_TOKEN")],
)
def test_missing_credential_refuses_to_start(mode: AuthMode, missing: str, tmp_path: Path) -> None:
    environ = {k: v for k, v in POLLUTED.items() if k != missing}
    with pytest.raises(ClaudeAuthError, match=missing) as info:
        auth.subprocess_environment(mode, environ, tmp_path)
    assert API_KEY not in str(info.value)
    assert OAUTH_TOKEN not in str(info.value)


def test_install_replaces_the_process_environment(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_dir = tmp_path / "claude"
    settings = Settings(database_url=URL, claude_auth_mode="oauth", claude_config_dir=config_dir)
    environ = dict(POLLUTED)
    with caplog.at_level(logging.WARNING):
        assert auth.install(settings, environ) is AuthMode.OAUTH
    assert environ == KEPT | {
        "CLAUDE_CODE_OAUTH_TOKEN": OAUTH_TOKEN,
        "CLAUDE_CONFIG_DIR": str(config_dir),
    }
    assert config_dir.stat().st_mode & 0o777 == 0o700
    assert "claude-auth.md" in caplog.text
    assert OAUTH_TOKEN not in caplog.text


def test_api_key_mode_logs_no_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(database_url=URL, claude_config_dir=tmp_path)
    with caplog.at_level(logging.WARNING):
        auth.install(settings, dict(POLLUTED))
    assert caplog.text == ""


def test_verify_environment_rejects_leftovers(tmp_path: Path) -> None:
    clean = auth.subprocess_environment(AuthMode.OAUTH, POLLUTED, tmp_path)
    auth.verify_environment(AuthMode.OAUTH, clean, tmp_path)
    with pytest.raises(ClaudeAuthError, match="ANTHROPIC_API_KEY"):
        auth.verify_environment(AuthMode.OAUTH, clean | {"ANTHROPIC_API_KEY": API_KEY}, tmp_path)
    with pytest.raises(ClaudeAuthError, match="CLAUDE_CONFIG_DIR"):
        auth.verify_environment(AuthMode.OAUTH, clean, tmp_path / "elsewhere")


@pytest.mark.parametrize(
    ("mode", "source", "ok"),
    [
        (AuthMode.API_KEY, "ANTHROPIC_API_KEY", True),
        (AuthMode.API_KEY, "none", False),
        (AuthMode.API_KEY, "apiKeyHelper", False),
        (AuthMode.OAUTH, "none", True),
        (AuthMode.OAUTH, "ANTHROPIC_API_KEY", False),
        (AuthMode.OAUTH, "apiKeyHelper", False),
        (AuthMode.OAUTH, "/login managed key", False),
        (AuthMode.OAUTH, None, False),
    ],
)
def test_check_init(mode: AuthMode, source: str | None, ok: bool) -> None:
    message = SystemMessage(subtype="init", data={"apiKeySource": source})
    if ok:
        assert auth.check_init(mode, message) == source
    else:
        with pytest.raises(ClaudeAuthError):
            auth.check_init(mode, message)


@pytest.mark.parametrize(("mode", "source"), [("api-key", "ANTHROPIC_API_KEY"), ("oauth", "none")])
def test_bundled_claude_code_reports_the_expected_source(
    mode: str, source: str, tmp_path: Path
) -> None:
    """Runs the SDK's bundled Claude Code with fake credentials and a polluted
    environment. The check stops at system/init, before any API request, so this
    pins the apiKeySource values whenever the SDK is upgraded."""
    env = POLLUTED | {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "HOME": str(tmp_path),
        "TITAN_CLAUDE_AUTH_MODE": mode,
        "TITAN_CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "titan.cli.main", "claude", "check"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ok: {mode} mode, credential source {source}"
    assert API_KEY not in result.stdout + result.stderr
    assert OAUTH_TOKEN not in result.stdout + result.stderr
