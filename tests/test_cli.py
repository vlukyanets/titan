from __future__ import annotations

import json
from pathlib import Path

from titan.cli.main import main


def test_openapi_export(tmp_path: Path) -> None:
    out = tmp_path / "openapi.json"
    assert main(["openapi", "--output", str(out)]) == 0
    schema = json.loads(out.read_text())
    assert schema["info"]["title"] == "TITAN API"
    assert "/api/v1/health" in schema["paths"]
