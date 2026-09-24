"""ADR 0004: the committed schema must match what the code generates."""

from __future__ import annotations

import json
from pathlib import Path

from titan.cli.main import openapi_schema

SCHEMA = Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.json"


def test_committed_openapi_schema_is_current() -> None:
    committed = json.loads(SCHEMA.read_text())
    assert committed == openapi_schema(), (
        "docs/api/openapi.json is stale; run `uv run titan openapi` and commit the result"
    )
