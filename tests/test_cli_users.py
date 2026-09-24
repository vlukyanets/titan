from __future__ import annotations

import io

import pytest

from titan.cli.main import main
from titan.settings import Settings

pytestmark = pytest.mark.db


def test_create_owner_and_list(db_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(database_url=db_url)
    owner = io.StringIO("owner-password-123\n")
    create = ["users", "create", "Anna", "--owner", "--password-stdin"]
    assert main(create, settings=settings, stdin=owner) == 0
    assert "created owner anna" in capsys.readouterr().out

    short = io.StringIO("short\n")
    create = ["users", "create", "boris", "--password-stdin"]
    assert main(create, settings=settings, stdin=short) == 1
    assert "at least 12 characters" in capsys.readouterr().err

    assert main(["users", "list"], settings=settings) == 0
    assert capsys.readouterr().out.splitlines() == ["anna\towner\tanna"]
