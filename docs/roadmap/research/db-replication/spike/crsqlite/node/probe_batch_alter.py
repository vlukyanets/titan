"""S5 extra probe, run inside a node container on throwaway files: does
Alembic's batch mode with a table copy (recreate="always", what SQLite needs for
DROP COLUMN / ALTER COLUMN) work on a cr-sqlite CRR inside
crsql_begin_alter / crsql_commit_alter, and do the two replicas still merge?"""

import json
import os
import sqlite3

import sqlalchemy as sa
import sqlite_vec
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, pool

EXT = os.environ.get("CRSQLITE_PATH", "/opt/ext/crsqlite")
out = {}


def raw(path):
    c = sqlite3.connect(path, isolation_level=None)
    c.enable_load_extension(True)
    c.load_extension(EXT)
    sqlite_vec.load(c)
    return c


def engine(path):
    e = sa.create_engine(f"sqlite:///{path}", poolclass=pool.NullPool)

    @event.listens_for(e, "connect")
    def _l(d, _):
        d.enable_load_extension(True)
        d.load_extension(EXT)

    @event.listens_for(e, "close")
    def _f(d, _):
        d.execute("SELECT crsql_finalize()")

    return e


paths = ["/tmp/probe_a.db", "/tmp/probe_b.db"]
for p in paths:
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(p + suffix):
            os.remove(p + suffix)
    c = raw(p)
    c.execute("CREATE TABLE t(id TEXT PRIMARY KEY NOT NULL, a TEXT NOT NULL DEFAULT '', b TEXT, c INTEGER NOT NULL DEFAULT 0)")
    c.execute("SELECT crsql_as_crr('t')")
    c.execute("SELECT crsql_finalize()")
    c.close()

a = raw(paths[0])
a.execute("INSERT INTO t(id, a, b, c) VALUES ('r1', 'x', 'y', 1)")
a.execute("SELECT crsql_finalize()")
a.close()

# Alembic batch recreate on replica A only: drop column b.
try:
    with engine(paths[0]).begin() as conn:
        conn.exec_driver_sql("SELECT crsql_begin_alter('t')")
        op = Operations(MigrationContext.configure(conn))
        with op.batch_alter_table("t", recreate="always") as bop:
            bop.drop_column("b")
        conn.exec_driver_sql("SELECT crsql_commit_alter('t')")
    out["batch_recreate_drop_column"] = "ok"
except Exception as e:  # noqa: BLE001
    out["batch_recreate_drop_column"] = f"error: {type(e).__name__}: {e}"[:400]

a = raw(paths[0])
out["a_schema"] = a.execute("SELECT sql FROM sqlite_master WHERE name='t'").fetchone()[0]
out["a_triggers"] = [r[0] for r in a.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='t'")]
try:
    a.execute("UPDATE t SET a='x2' WHERE id='r1'")
    ch = a.execute('SELECT "table","pk","cid","val","col_version","db_version","site_id","cl","seq" FROM crsql_changes').fetchall()
    out["a_changes_after_recreate"] = [[r[2], r[3], r[4]] for r in ch]
    b = raw(paths[1])
    b.execute("BEGIN")
    try:
        b.executemany('INSERT INTO crsql_changes("table","pk","cid","val","col_version","db_version","site_id","cl","seq") VALUES (?,?,?,?,?,?,?,?,?)', ch)
        b.execute("COMMIT")
        out["merge_into_b_(old_schema)"] = b.execute("SELECT * FROM t").fetchall()
    except Exception as e:  # noqa: BLE001
        b.execute("ROLLBACK")
        out["merge_into_b_(old_schema)"] = f"error: {type(e).__name__}: {e}"
    b.execute("SELECT crsql_finalize()")
    b.close()
except Exception as e:  # noqa: BLE001
    out["a_after_recreate"] = f"error: {type(e).__name__}: {e}"
a.execute("SELECT crsql_finalize()")
a.close()
print(json.dumps(out, default=str))
