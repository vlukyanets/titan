"""Scenarios S1–S8 from docs/roadmap/plans/m0-db-replication.md.

Every scenario returns a JSON-serialisable dict. Node roles: n1 home server, n2 VPS,
n3 laptop. "App on node k" means the connection an API process on that node would use.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import time
import uuid
from typing import Any

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from spike.cluster import (
    NODES,
    Candidate,
    Faults,
    attempt,
    dir_bytes,
    engine,
    execute,
    scalar,
    sh,
    wait_until,
)
from spike.models import DIM, Embedding, Job, Task

ALL = (1, 2, 3)


# --------------------------------------------------------------------------- health


def patroni_members(c: Candidate) -> list[dict[str, Any]]:
    for node in NODES:
        out = sh(
            "docker", "exec", c.container(node), "patronictl", "-c", "/tmp/patroni.yml",
            "list", "-f", "json", check=False,
        )
        if out.strip():
            return json.loads(out)
    return []


def yb_underreplicated(c: Candidate) -> int:
    for node in NODES:
        out = sh(
            "docker", "exec", c.container(node), "curl", "-s", "--max-time", "5",
            f"http://n{node}:7000/api/v1/tablet-under-replication", check=False,
        )
        if out.strip().startswith("{"):
            return len(json.loads(out).get("underreplicated_tablets", []))
    return -1


async def healthy(c: Candidate) -> bool:
    if c.name == "spock":
        for node in NODES:
            e = engine(c, node)
            try:
                bad = await scalar(
                    e, "select count(*) from spock.sub_show_status() where status <> 'replicating'"
                )
            finally:
                await e.dispose()
            if bad:
                return False
        return True
    if c.name == "patroni":
        members = {m["Member"]: m for m in patroni_members(c)}
        if members.get("n1", {}).get("Role") != "Leader":
            return False
        return all(
            members.get(n, {}).get("State") == "streaming" for n in ("n2", "n3")
        )
    if c.name == "yugabyte":
        return yb_underreplicated(c) == 0
    raise ValueError(c.name)


async def prepare(c: Candidate, faults: Faults, timeout: float = 300) -> float | None:
    """Heal everything and wait until the cluster is back to its normal layout."""
    faults.heal()
    for node in NODES:
        faults.unpause(node)
    if c.name == "patroni":
        leader = next((m["Member"] for m in patroni_members(c) if m["Role"] == "Leader"), None)
        if leader and leader != "n1":
            await wait_until(lambda: _patroni_n1_ready(c), 180, 2)
            sh(
                "docker", "exec", c.container(1), "patronictl", "-c", "/tmp/patroni.yml",
                "switchover", "--leader", leader, "--candidate", "n1", "--force", check=False,
            )
    return await wait_until(lambda: healthy(c), timeout, 2)


async def _patroni_n1_ready(c: Candidate) -> bool:
    members = {m["Member"]: m for m in patroni_members(c)}
    return members.get("n1", {}).get("State") in ("streaming", "running")


# --------------------------------------------------------------------------- helpers


async def retry_write(eng: AsyncEngine, sql: str, deadline: float, **params: Any) -> dict[str, Any]:
    """Retry a write until it succeeds or the deadline passes, as an API with retries would."""
    start = time.monotonic()
    tries, last = 0, ""
    while time.monotonic() - start < deadline:
        tries += 1
        res = await attempt(execute(eng, sql, timeout=10, **params))
        if res["ok"]:
            return {"ok": True, "seconds": round(time.monotonic() - start, 1), "tries": tries}
        last = res["error"]
        await asyncio.sleep(1)
    return {"ok": False, "seconds": round(time.monotonic() - start, 1), "tries": tries, "error": last}


async def rows_everywhere(c: Candidate, sql: str, **params: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for node in NODES:
        e = engine(c, node)
        try:
            async with e.connect() as conn:
                res = await asyncio.wait_for(conn.execute(text(sql), params), 15)
                out[f"n{node}"] = [list(map(str, r)) for r in res.all()]
        except Exception as exc:  # noqa: BLE001
            out[f"n{node}"] = f"error: {type(exc).__name__}"
        finally:
            await e.dispose()
    return out


def converged(snapshot: dict[str, Any]) -> bool:
    values = list(snapshot.values())
    return all(not isinstance(v, str) for v in values) and all(v == values[0] for v in values)


async def wal_retained(c: Candidate) -> dict[str, int]:
    """Bytes of WAL each Postgres node keeps for its replication slots."""
    out: dict[str, int] = {}
    for node in NODES:
        e = engine(c, node)
        try:
            out[f"n{node}"] = int(
                await scalar(
                    e,
                    "select coalesce(sum(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)), 0)"
                    " from pg_replication_slots where not pg_is_in_recovery()",
                    timeout=10,
                )
                or 0
            )
        except Exception:  # noqa: BLE001 - paused or replica node
            out[f"n{node}"] = -1
        finally:
            await e.dispose()
    return out


# --------------------------------------------------------------------------- S1


async def s1_conflicts(c: Candidate, faults: Faults) -> dict[str, Any]:
    """Conflicting writes on both sides of a partition that isolates the home server."""
    run = uuid.uuid4().hex[:8]
    t_key, u_key, d_key = f"t-{run}", f"u-{run}", f"d-{run}"
    app_all = engine(c, 1, reachable=ALL)
    for key in (t_key, u_key):
        await execute(
            app_all,
            "insert into tasks (id, external_key, title, attrs, origin) "
            "values (:id, :k, 'orig', '{}', 'n1')",
            id=uuid.uuid4(), k=key,
        )
    await app_all.dispose()
    probe_sql = "select external_key, title, coalesce(notes, '-'), origin from tasks " \
        "where external_key in (:t, :u, :d) order by external_key, origin"
    params = {"t": t_key, "u": u_key, "d": d_key}
    await wait_until(lambda: _conv(c, probe_sql, params, 2), 60)

    faults.isolate(1)
    await asyncio.sleep(2)
    side1 = engine(c, 1, reachable=(1,))
    side2 = engine(c, 2, reachable=(2, 3))

    async def left() -> list[dict[str, Any]]:
        return [
            await retry_write(side1, "update tasks set title = 'n1' where external_key = :k",
                              45, k=t_key),
            await retry_write(side1, "update tasks set title = 'n1' where external_key = :k",
                              45, k=u_key),
            await retry_write(
                side1,
                "insert into tasks (id, external_key, title, attrs, origin) "
                "values (:id, :k, 'dup', '{}', 'n1')",
                45, id=uuid.uuid4(), k=d_key,
            ),
        ]

    async def right() -> list[dict[str, Any]]:
        await asyncio.sleep(1)  # the n2 side writes last, so last-writer-wins should pick it
        return [
            await retry_write(side2, "update tasks set notes = 'n2' where external_key = :k",
                              60, k=t_key),
            await retry_write(side2, "update tasks set title = 'n2' where external_key = :k",
                              60, k=u_key),
            await retry_write(
                side2,
                "insert into tasks (id, external_key, title, attrs, origin) "
                "values (:id, :k, 'dup', '{}', 'n2')",
                60, id=uuid.uuid4(), k=d_key,
            ),
        ]

    n1_ops, n2_ops = await asyncio.gather(left(), right())
    await side1.dispose()
    await side2.dispose()
    faults.heal()
    healed = await wait_until(lambda: _conv(c, probe_sql, params, 3), 180, 2)
    final = await rows_everywhere(c, probe_sql, **params)
    result: dict[str, Any] = {
        "ops_on_isolated_n1": dict(zip(["T.title", "U.title", "insert dup"], n1_ops)),
        "ops_on_majority_n2": dict(zip(["T.notes", "U.title", "insert dup"], n2_ops)),
        "converged_after_heal_s": healed,
        "final_rows_per_node": final,
    }
    if c.name == "spock":
        e = engine(c, 1)
        try:
            result["spock_resolutions"] = await scalar(e, "select count(*) from spock.resolutions")
            result["spock_exceptions"] = await scalar(e, "select count(*) from spock.exception_log")
        finally:
            await e.dispose()
    return result


async def _conv(c: Candidate, sql: str, params: dict[str, Any], min_rows: int) -> bool:
    snap = await rows_everywhere(c, sql, **params)
    first = next(iter(snap.values()))
    return converged(snap) and isinstance(first, list) and len(first) >= min_rows


# --------------------------------------------------------------------------- S2


async def s2_laptop_away(
    c: Candidate, faults: Faults, pause_s: int = 180, rows: int = 20_000
) -> dict[str, Any]:
    """The laptop sleeps past every eviction timer while the other nodes keep writing."""
    app = engine(c, 1, reachable=(1, 2))
    before = int(await scalar(app, "select count(*) from tasks"))
    disk_before = {f"n{n}": dir_bytes(c, n) for n in NODES}
    faults.pause(3)
    paused_at = time.monotonic()

    write_start = time.monotonic()
    batch = 500
    for i in range(0, rows, batch):
        async with app.begin() as conn:
            await conn.execute(
                text(
                    "insert into tasks (id, title, notes, attrs, origin) "
                    "values (:id, :title, :notes, '{\"src\": \"s2\"}', 'n1')"
                ),
                [
                    {"id": uuid.uuid4(), "title": f"away {i + j}", "notes": "x" * 200}
                    for j in range(batch)
                ],
            )
    write_s = round(time.monotonic() - write_start, 1)
    expected = before + rows
    backlog_during = await wal_retained(c) if c.name != "yugabyte" else None

    remaining = pause_s - (time.monotonic() - paused_at)
    if remaining > 0:
        await asyncio.sleep(remaining)
    backlog_end = await wal_retained(c) if c.name != "yugabyte" else None
    disk_end = {f"n{n}": dir_bytes(c, n) for n in (1, 2)}
    await app.dispose()

    faults.unpause(3)
    if c.name == "yugabyte":
        caught_up = await wait_until(lambda: _yb_ok(c), 900, 3)
    else:
        caught_up = await wait_until(lambda: _count_on(c, 3, expected), 900, 2)

    # Fault tolerance check: lose the home server right after the laptop returned.
    faults.pause(1)
    laptop_app = engine(c, 3, reachable=(2, 3))
    write_after = await retry_write(
        laptop_app,
        "insert into tasks (id, title, attrs, origin) values (:id, 'after', '{}', 'n3')",
        180, id=uuid.uuid4(),
    )
    seen_on_laptop = await attempt(scalar(engine(c, 3), "select count(*) from tasks"))
    await laptop_app.dispose()
    faults.unpause(1)
    return {
        "rows_written_while_away": rows,
        "write_seconds": write_s,
        "pause_seconds": pause_s,
        "wal_retained_bytes_after_writes": backlog_during,
        "wal_retained_bytes_at_return": backlog_end,
        "data_dir_bytes_before": disk_before,
        "data_dir_bytes_at_return": disk_end,
        "laptop_caught_up_s": caught_up,
        "write_with_home_down_after_return": write_after,
        "rows_seen_on_laptop": seen_on_laptop,
        "rows_expected_on_laptop": expected + (1 if write_after["ok"] else 0),
    }


async def _count_on(c: Candidate, node: int, expected: int) -> bool:
    e = engine(c, node)
    try:
        return int(await scalar(e, "select count(*) from tasks", timeout=10)) >= expected
    finally:
        await e.dispose()


async def _yb_ok(c: Candidate) -> bool:
    return yb_underreplicated(c) == 0


# --------------------------------------------------------------------------- S3

CLAIM = """
update jobs set lease_owner = :me, lease_until = now() + interval '30 seconds'
where id in (
  select id from jobs
  where batch = :batch and fired_at is null and due_at <= now()
    and (lease_until is null or lease_until < now())
  order by due_at limit 5
  for update skip locked
)
returning id
"""


async def _worker(
    name: str, eng: AsyncEngine, batch: str, sent: list[tuple[str, str]], seconds: float
) -> dict[str, int]:
    stats = {"claims": 0, "errors": 0}
    stop = time.monotonic() + seconds
    while time.monotonic() < stop:
        try:
            async with eng.begin() as conn:
                ids = [r[0] for r in (await asyncio.wait_for(
                    conn.execute(text(CLAIM), {"me": name, "batch": batch}), 10)).all()]
            if not ids:
                async with eng.connect() as conn:
                    left = (await asyncio.wait_for(conn.execute(text(
                        "select count(*) from jobs where batch = :b and fired_at is null"),
                        {"b": batch}), 10)).scalar()
                if not left:
                    break
                await asyncio.sleep(0.5)
                continue
            stats["claims"] += 1
            # The push notification leaves the system here; this list is the outside world.
            sent.extend((str(i), name) for i in ids)
            async with eng.begin() as conn:
                await asyncio.wait_for(conn.execute(
                    text("update jobs set fired_at = now(), fired_by = :me where id = any(:ids)"),
                    {"me": name, "ids": ids}), 10)
        except Exception:  # noqa: BLE001 - counted, then retried like a real worker
            stats["errors"] += 1
            await asyncio.sleep(0.5)
    return stats


async def _jobs(c: Candidate, batch: str, n: int) -> None:
    app = engine(c, 1, reachable=ALL)
    async with app.begin() as conn:
        await conn.execute(
            text("insert into jobs (id, batch, due_at) values (:id, :b, now() - interval '1 second')"),
            [{"id": uuid.uuid4(), "b": batch} for _ in range(n)],
        )
    await app.dispose()
    sql = "select count(*) from jobs where batch = :b"
    await wait_until(lambda: _conv(c, sql, {"b": batch}, 1), 60)


def _fires(sent: list[tuple[str, str]], n: int) -> dict[str, int]:
    per_job: dict[str, int] = {}
    for job, _ in sent:
        per_job[job] = per_job.get(job, 0) + 1
    return {
        "jobs": n,
        "fired_once": sum(1 for v in per_job.values() if v == 1),
        "fired_twice_or_more": sum(1 for v in per_job.values() if v > 1),
        "never_fired": n - len(per_job),
    }


async def s3_exactly_once(c: Candidate, faults: Faults, n: int = 300) -> dict[str, Any]:
    """Two workers race for the same reminders, first healthy, then across a partition."""
    out: dict[str, Any] = {}

    batch = f"h-{uuid.uuid4().hex[:6]}"
    await _jobs(c, batch, n)
    sent: list[tuple[str, str]] = []
    e1, e2 = engine(c, 1, reachable=ALL), engine(c, 2, reachable=ALL)
    stats = await asyncio.gather(
        _worker("w1", e1, batch, sent, 60), _worker("w2", e2, batch, sent, 60)
    )
    await e1.dispose()
    await e2.dispose()
    out["healthy"] = {**_fires(sent, n), "worker_stats": stats}

    await prepare(c, faults)
    batch = f"p-{uuid.uuid4().hex[:6]}"
    await _jobs(c, batch, n)
    sent = []
    faults.isolate(1)
    await asyncio.sleep(1)
    e1, e2 = engine(c, 1, reachable=(1,)), engine(c, 2, reachable=(2, 3))
    stats = await asyncio.gather(
        _worker("w1", e1, batch, sent, 75), _worker("w2", e2, batch, sent, 75)
    )
    await e1.dispose()
    await e2.dispose()
    faults.heal()
    await prepare(c, faults)
    # After the partition heals, a worker picks up anything the minority side leased.
    e1 = engine(c, 1, reachable=ALL)
    stats_after = await _worker("w1", e1, batch, sent, 60)
    await e1.dispose()
    out["partitioned"] = {
        **_fires(sent, n),
        "worker_stats": stats,
        "cleanup_worker_after_heal": stats_after,
    }
    return out


# --------------------------------------------------------------------------- S4


def _dataset(n: int, q: int, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    centroids = rng.standard_normal((1000, DIM)).astype(np.float32)
    data = centroids[rng.integers(0, 1000, n)] + 0.6 * rng.standard_normal((n, DIM)).astype(
        np.float32
    )
    data /= np.linalg.norm(data, axis=1, keepdims=True)
    queries = data[rng.integers(0, n, q)] + 0.2 * rng.standard_normal((q, DIM)).astype(np.float32)
    queries /= np.linalg.norm(queries, axis=1, keepdims=True)
    return data, queries


async def s4_vectors(c: Candidate, faults: Faults, n: int = 100_000, q: int = 200) -> dict[str, Any]:
    import asyncpg
    from pgvector.asyncpg import register_vector

    data, queries = _dataset(n, q)
    truth = np.argsort(-(queries @ data.T), axis=1)[:, :10]
    primary = 1
    dsn = c.url(primary).replace(f"{c.scheme}+asyncpg", "postgresql")
    conn = await asyncpg.connect(dsn, timeout=10, server_settings={"statement_timeout": "0"})
    await register_vector(conn)
    await conn.execute("drop table if exists vec_bench")
    await conn.execute(f"create table vec_bench (id int primary key, embedding vector({DIM}))")
    await asyncio.sleep(2)  # let replicated DDL land before rows arrive
    t0 = time.monotonic()
    step = 2_000
    for i in range(0, n, step):
        records = [(i + j, data[i + j]) for j in range(min(step, n - i))]
        await conn.copy_records_to_table("vec_bench", records=records, columns=["id", "embedding"])
    load_s = round(time.monotonic() - t0, 1)

    replicated = await wait_until(lambda: _vec_count(c, 3, n), 900, 2)

    await conn.execute("set maintenance_work_mem = '1GB'")
    using = "ybhnsw" if c.name == "yugabyte" else "hnsw"
    t0 = time.monotonic()
    await conn.execute(
        f"create index vec_bench_ann on vec_bench using {using} (embedding vector_l2_ops) "
        "with (m = 16, ef_construction = 64)",
        timeout=3600,
    )
    build_s = round(time.monotonic() - t0, 1)
    index_on_laptop = await wait_until(lambda: _has_index(c, 3, "vec_bench_ann"), 900, 2)

    results: dict[str, Any] = {}
    for ef in (40, 100):
        await conn.execute(f"set hnsw.ef_search = {ef}")
        lat, hits = [], 0
        for qi in range(q):
            t = time.perf_counter()
            rows = await conn.fetch(
                "select id from vec_bench order by embedding <-> $1 limit 10", queries[qi]
            )
            lat.append((time.perf_counter() - t) * 1000)
            hits += len({r["id"] for r in rows} & set(truth[qi].tolist()))
        plan = await conn.fetchval(
            "explain (format text) select id from vec_bench order by embedding <-> $1 limit 10",
            queries[0],
        )
        results[f"ef_search={ef}"] = {
            "recall_at_10": round(hits / (q * 10), 3),
            "p50_ms": round(statistics.median(lat), 2),
            "p95_ms": round(float(np.percentile(lat, 95)), 2),
            "uses_index": "vec_bench_ann" in str(plan) or "ann" in str(plan).lower(),
        }
    table_bytes = await conn.fetchval("select pg_total_relation_size('vec_bench')")
    await conn.close()
    return {
        "rows": n,
        "dim": DIM,
        "load_seconds": load_s,
        "replicated_to_laptop_s": replicated,
        "index_build_seconds": build_s,
        "index_present_on_laptop_s": index_on_laptop,
        "total_relation_bytes_primary": table_bytes,
        "search": results,
    }


async def _vec_count(c: Candidate, node: int, n: int) -> bool:
    e = engine(c, node)
    try:
        return int(await scalar(e, "select count(*) from vec_bench", timeout=60)) >= n
    finally:
        await e.dispose()


async def _has_index(c: Candidate, node: int, name: str) -> bool:
    e = engine(c, node)
    try:
        return bool(await scalar(e, "select count(*) from pg_indexes where indexname = :n", n=name))
    finally:
        await e.dispose()


# --------------------------------------------------------------------------- S5


def _alembic(c: Candidate, url: str, *args: str) -> dict[str, Any]:
    t0 = time.monotonic()
    res = subprocess.run(
        ["alembic", *args], capture_output=True, text=True, env={**os.environ, "SPIKE_URL": url},
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), timeout=900,
    )
    return {
        "ok": res.returncode == 0,
        "seconds": round(time.monotonic() - t0, 1),
        "output": (res.stdout + res.stderr).strip().splitlines()[-3:],
    }


async def _schema_on(c: Candidate, node: int) -> dict[str, Any]:
    e = engine(c, node)
    try:
        return {
            "version": await scalar(e, "select version_num from alembic_version"),
            "priority_column": bool(await scalar(
                e, "select count(*) from information_schema.columns "
                   "where table_name = 'tasks' and column_name = 'priority'")),
            "ann_index": bool(await scalar(
                e, "select count(*) from pg_indexes where indexname = 'ix_embeddings_ann'")),
        }
    finally:
        await e.dispose()


async def s5_alembic(c: Candidate, faults: Faults) -> dict[str, Any]:
    """Apply a revision on one node only and watch it reach the others."""
    url = c.app_url(1, ALL) if c.extra.get("single_primary") else c.url(1)
    steps: dict[str, Any] = {}
    steps["upgrade_head_on_n1"] = _alembic(c, url, "upgrade", "head")
    steps["reached_laptop_s"] = await wait_until(
        lambda: _schema_is(c, 3, "0002", True), 120, 1)
    steps["schema_per_node"] = {f"n{n}": await _schema_on(c, n) for n in NODES}
    app2 = engine(c, 2, reachable=ALL)
    steps["insert_new_column_via_n2"] = await attempt(execute(
        app2, "insert into tasks (id, title, attrs, origin, priority) "
              "values (:id, 's5', '{}', 'n2', 1)", id=uuid.uuid4()))
    await app2.dispose()
    steps["alembic_check_on_n3"] = _alembic(c, c.url(3), "check")
    steps["downgrade_on_n1"] = _alembic(c, url, "downgrade", "0001")
    steps["downgrade_reached_laptop_s"] = await wait_until(
        lambda: _schema_is(c, 3, "0001", False), 120, 1)
    steps["upgrade_again_on_n1"] = _alembic(c, url, "upgrade", "head")
    steps["upgrade_again_reached_laptop_s"] = await wait_until(
        lambda: _schema_is(c, 3, "0002", True), 120, 1)
    return steps


async def _schema_is(c: Candidate, node: int, version: str, column: bool) -> bool:
    s = await _schema_on(c, node)
    return s["version"] == version and s["priority_column"] == column


# --------------------------------------------------------------------------- S6


async def s6_idle(c: Candidate, faults: Faults, settle: int = 60) -> dict[str, Any]:
    await asyncio.sleep(settle)
    samples: dict[str, list[tuple[float, float]]] = {c.container(n): [] for n in NODES}
    for _ in range(3):
        out = sh("docker", "stats", "--no-stream", "--format", "{{json .}}", *samples)
        for line in out.splitlines():
            row = json.loads(line)
            mem = row["MemUsage"].split("/")[0].strip()
            samples[row["Name"]].append((float(row["CPUPerc"].rstrip("%")), _mib(mem)))
        await asyncio.sleep(5)
    return {
        name: {
            "cpu_percent_avg": round(statistics.mean(s[0] for s in vals), 2),
            "mem_mib_avg": round(statistics.mean(s[1] for s in vals), 1),
        }
        for name, vals in samples.items()
    }


def _mib(value: str) -> float:
    units = {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024, "B": 1 / 1024 / 1024}
    for unit, factor in units.items():
        if value.endswith(unit):
            return float(value[: -len(unit)]) * factor
    return float(value)


# --------------------------------------------------------------------------- S7


async def s7_orm(c: Candidate, faults: Faults) -> dict[str, Any]:
    """SQLAlchemy 2 async ORM features the domains will rely on, per driver."""
    out: dict[str, Any] = {}
    for variant, driver, codec in (
        ("asyncpg", "asyncpg", True),
        ("asyncpg_without_vector_codec", "asyncpg", False),
        ("psycopg", "psycopg", False),
    ):
        eng = engine(c, 2, driver=driver, reachable=ALL, vector_codec=codec)
        Session = async_sessionmaker(eng, expire_on_commit=False)
        checks: dict[str, Any] = {}
        key = f"orm-{variant[:12]}-{uuid.uuid4().hex[:6]}"
        vec = np.random.default_rng(1).standard_normal(DIM).astype(np.float32)
        vec /= np.linalg.norm(vec)

        async def insert_select() -> str:
            async with Session() as s:
                t = Task(external_key=key, title="orm", attrs={"k": "v"}, origin="n2")
                s.add(t)
                await s.commit()
                got = await s.get(Task, t.id)
                assert got is not None and got.updated_at is not None
                return "server_default updated_at populated"

        async def jsonb_filter() -> int:
            async with Session() as s:
                rows = (await s.scalars(
                    select(Task).where(Task.external_key == key, Task.attrs["k"].astext == "v")
                )).all()
                assert len(rows) == 1
                return len(rows)

        async def upsert_returning() -> str:
            async with Session() as s:
                stmt = pg_insert(Task).values(
                    id=uuid.uuid4(), external_key=key, title="upserted", attrs={}, origin="n2"
                ).on_conflict_do_update(
                    index_elements=[Task.external_key], set_={"title": "upserted"}
                ).returning(Task.title)
                title = (await s.execute(stmt)).scalar_one()
                await s.commit()
                assert title == "upserted"
                return title

        async def vector_roundtrip() -> str:
            async with Session() as s:
                s.add(Embedding(chunk=0, embedding=vec.tolist()))
                await s.commit()
                nearest = (await s.scalars(
                    select(Embedding).order_by(Embedding.embedding.l2_distance(vec.tolist())).limit(1)
                )).first()
                assert nearest is not None
                assert np.allclose(np.asarray(nearest.embedding), vec, atol=1e-6)
                return "l2_distance order_by and round trip exact"

        async def skip_locked() -> str:
            async with Session() as s:
                s.add_all([Job(batch=key[:32], due_at=_now()) for _ in range(2)])
                await s.commit()
            async with Session() as a, Session() as b:
                first = (await a.scalars(select(Job).where(Job.batch == key[:32])
                                         .with_for_update(skip_locked=True).limit(1))).first()
                second = (await b.scalars(select(Job).where(Job.batch == key[:32])
                                          .with_for_update(skip_locked=True).limit(1))).first()
                assert first is not None and second is not None and first.id != second.id
                await a.rollback()
                await b.rollback()
                return "second session skipped the locked row"

        async def savepoint() -> str:
            async with Session() as s:
                async with s.begin():
                    s.add(Task(title="outer", attrs={}, origin="n2"))
                    nested = await s.begin_nested()
                    s.add(Task(title="inner", attrs={}, origin="n2"))
                    await nested.rollback()
                return "nested transaction rolled back alone"

        for name, fn in (
            ("insert_select", insert_select),
            ("jsonb_filter", jsonb_filter),
            ("upsert_returning", upsert_returning),
            ("vector_roundtrip", vector_roundtrip),
            ("skip_locked", skip_locked),
            ("savepoint", savepoint),
        ):
            checks[name] = await attempt(asyncio.wait_for(fn(), 30))
        await eng.dispose()
        out[variant] = checks
    return out


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(UTC)


# --------------------------------------------------------------------------- S8


async def s8_backup(c: Candidate, faults: Faults) -> dict[str, Any]:
    e = engine(c, 1)
    expected = int(await scalar(e, "select count(*) from tasks"))
    await e.dispose()
    if c.name in ("spock", "patroni"):
        node = 1
        env = ["-e", "PGPASSWORD=spike-password"]
        dump = attempt_sync(lambda: sh(
            "docker", "exec", *env, c.container(node), "bash", "-c",
            "pg_dump -h localhost -U titan -d titan -Fc --exclude-extension=spock "
            "--exclude-schema=spock -f /tmp/titan.dump && ls -l /tmp/titan.dump"))
        restore = attempt_sync(lambda: sh(
            "docker", "exec", *env, c.container(node), "bash", "-c",
            "psql -h localhost -U titan -d postgres -qc 'drop database if exists restore_check' "
            "-c 'create database restore_check' && "
            "pg_restore -h localhost -U titan -d restore_check --no-owner /tmp/titan.dump && "
            "psql -h localhost -U titan -d restore_check -qAtc 'select count(*) from tasks'"))
        # Spock would replicate CREATE DATABASE? It does not; it is filtered. Drop the copy.
        sh("docker", "exec", *env, c.container(node), "psql", "-h", "localhost", "-U", "titan",
           "-d", "postgres", "-qc", "drop database if exists restore_check", check=False)
    else:
        dump = attempt_sync(lambda: sh(
            "docker", "exec", c.container(1), "bash", "-c",
            "bin/ysql_dump -h n1 -U yugabyte -d yugabyte -f /tmp/yb.sql && ls -l /tmp/yb.sql"))
        restore = attempt_sync(lambda: sh(
            "docker", "exec", c.container(1), "bash", "-c",
            "bin/ysqlsh -h n1 -U yugabyte -qc 'drop database if exists restore_check' "
            "-c 'create database restore_check' && "
            "bin/ysqlsh -h n1 -U yugabyte -d restore_check -q -f /tmp/yb.sql > /dev/null && "
            "bin/ysqlsh -h n1 -U yugabyte -d restore_check -qAtc 'select count(*) from tasks'"))
        snap = attempt_sync(lambda: sh(
            "docker", "exec", c.container(1), "bin/yb-admin", "--master_addresses",
            "n1:7100,n2:7100,n3:7100", "create_database_snapshot", "ysql.yugabyte"))
        sh("docker", "exec", c.container(1), "bin/ysqlsh", "-h", "n1", "-U", "yugabyte", "-qc",
           "drop database if exists restore_check", check=False)
        return {"expected_tasks": expected, "dump": dump, "restore": restore, "snapshot": snap}
    return {"expected_tasks": expected, "dump": dump, "restore": restore}


def attempt_sync(fn: Any) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        out = fn()
        return {"ok": True, "seconds": round(time.monotonic() - t0, 1),
                "output": out.strip().splitlines()[-2:]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "seconds": round(time.monotonic() - t0, 1), "error": str(exc)[:300]}


SCENARIOS = {
    "s1": s1_conflicts,
    "s2": s2_laptop_away,
    "s3": s3_exactly_once,
    "s4": s4_vectors,
    "s5": s5_alembic,
    "s6": s6_idle,
    "s7": s7_orm,
    "s8": s8_backup,
}
