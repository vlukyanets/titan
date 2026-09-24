from __future__ import annotations

import io

import pytest

from titan.cli.main import main
from titan.settings import Settings

pytestmark = pytest.mark.db


def test_send_stores_the_notification(db_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(database_url=db_url)
    password = io.StringIO("owner-password-123\n")
    create = ["users", "create", "anna", "--owner", "--password-stdin"]
    assert main(create, settings=settings, stdin=password) == 0
    capsys.readouterr()

    send = ["notifications", "send", "Anna", "Backup finished", "--body", "3 GB"]
    assert main(send, settings=settings) == 0
    out = capsys.readouterr().out
    assert out.startswith("sent ")
    assert out.rstrip().endswith("to anna: stored, no push got through")

    assert main(["notifications", "send", "nobody", "hello"], settings=settings) == 1
    assert "user not found" in capsys.readouterr().err
