from __future__ import annotations

import io

import pytest

from titan.cli.main import main

pytestmark = pytest.mark.db


def test_create_owner_and_list(
    db_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TITAN_DATABASE_URL", db_url)
    monkeypatch.setattr("sys.stdin", io.StringIO("owner-password-123\n"))
    assert main(["users", "create", "Anna", "--owner", "--password-stdin"]) == 0
    assert "created owner anna" in capsys.readouterr().out

    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["users", "create", "boris", "--password-stdin"]) == 1
    assert "at least 12 characters" in capsys.readouterr().err

    assert main(["users", "list"]) == 0
    assert capsys.readouterr().out.splitlines() == ["anna\towner\tanna"]
