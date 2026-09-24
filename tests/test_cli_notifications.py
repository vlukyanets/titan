from __future__ import annotations

import io

import pytest

from titan.cli.main import main

pytestmark = pytest.mark.db


def test_send_stores_the_notification(
    db_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TITAN_DATABASE_URL", db_url)
    monkeypatch.setattr("sys.stdin", io.StringIO("owner-password-123\n"))
    assert main(["users", "create", "anna", "--owner", "--password-stdin"]) == 0
    capsys.readouterr()

    assert main(["notifications", "send", "Anna", "Backup finished", "--body", "3 GB"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("sent ")
    assert out.rstrip().endswith("to anna: stored, no push got through")

    assert main(["notifications", "send", "nobody", "hello"]) == 1
    assert "user not found" in capsys.readouterr().err
