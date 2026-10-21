"""The node's compose file and image keep the database private."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent.parent


def compose_config() -> dict[str, Any]:
    """The compose file as Docker reads it."""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        env={**os.environ, "POSTGRES_PASSWORD": "x"},
        capture_output=True,
        text=True,
        check=True,
    )
    config: dict[str, Any] = json.loads(result.stdout)
    return config


def test_only_the_app_has_a_host_port() -> None:
    services = compose_config()["services"]
    assert "ports" not in services["db"]
    assert services["app"]["ports"]


def test_database_keeps_its_data_in_a_volume() -> None:
    volumes = compose_config()["services"]["db"]["volumes"]
    assert [(v["type"], v["target"]) for v in volumes] == [
        ("volume", "/var/lib/postgresql")
    ]


def test_app_runs_as_a_non_root_user() -> None:
    lines = (ROOT / "backend" / "Dockerfile").read_text().splitlines()
    users = [line.split()[1] for line in lines if line.startswith("USER ")]
    assert users and users[-1] not in ("root", "0")
