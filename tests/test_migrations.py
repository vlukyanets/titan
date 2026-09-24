"""Migration round trip from docs/architecture/database-migrations.md."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from titan.cli.main import alembic_config

pytestmark = pytest.mark.db


def test_upgrade_downgrade_upgrade_and_no_drift(db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    # Fails when the models and the migrated schema differ.
    command.check(cfg)


def test_at_most_one_head() -> None:
    heads = ScriptDirectory.from_config(alembic_config()).get_heads()
    assert len(heads) <= 1, f"multiple Alembic heads: {heads}"
