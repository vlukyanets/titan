from __future__ import annotations

import json
from pathlib import Path

import pytest

from titan.cli.main import main


def test_openapi_export(tmp_path: Path) -> None:
    out = tmp_path / "openapi.json"
    assert main(["openapi", "--output", str(out)]) == 0
    schema = json.loads(out.read_text())
    assert schema["info"]["title"] == "TITAN API"
    assert "/api/v1/health" in schema["paths"]


def test_node_commands_without_settings_name_what_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TITAN_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["users", "list"])
    assert "TITAN_DATABASE_URL" in str(exc.value)
    assert "docker compose exec api titan" in str(exc.value)
