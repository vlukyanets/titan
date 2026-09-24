#!/bin/sh
# Startup migration: a node that has already been bootstrapped (it has an
# alembic_version table) upgrades itself to the revisions shipped in the image.
# cr-sqlite does not replicate DDL, so every node must run Alembic locally.
set -e
if [ "${MIGRATE_ON_START:-0}" = "1" ] && [ -f "$TITAN_DB_PATH" ]; then
  if python - <<'PY'
import os, sqlite3, sys
c = sqlite3.connect(os.environ["TITAN_DB_PATH"])
r = c.execute("select 1 from sqlite_master where name='alembic_version'").fetchone()
sys.exit(0 if r else 1)
PY
  then
    echo "entrypoint: alembic upgrade head"
    (cd /app && alembic upgrade head)
  fi
fi
exec python /app/node.py
