"""Engine detection for the few statements that differ between candidates."""

from alembic import op


def flavour() -> str:
    bind = op.get_bind()
    if bind.dialect.name == "cockroachdb":
        return "cockroach"
    version = bind.exec_driver_sql("select version()").scalar() or ""
    return "yugabyte" if "-YB-" in version else "postgres"
