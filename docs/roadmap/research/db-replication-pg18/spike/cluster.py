"""Candidate definitions, engines and fault injection for the three-node clusters."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

NETSHOOT = "nicolaka/netshoot:latest"
NODES = (1, 2, 3)  # n1 = home server, n2 = VPS, n3 = laptop


@dataclass(frozen=True)
class Candidate:
    name: str
    scheme: str  # SQLAlchemy dialect prefix without driver
    user: str
    password: str
    database: str
    ports: tuple[int, int, int]
    data_dir: str
    # Raft engines answer every read through the leaseholder, so "caught up" has to be
    # read from replication health rather than from row counts on the returning node.
    consensus: bool
    extra: dict[str, Any] = field(default_factory=dict)

    def container(self, node: int) -> str:
        return f"{self.name}-n{node}"

    def url(self, node: int, driver: str = "asyncpg") -> str:
        auth = self.user if not self.password else f"{self.user}:{self.password}"
        return f"{self.scheme}+{driver}://{auth}@127.0.0.1:{self.ports[node - 1]}/{self.database}"

    def app_url(self, node: int, reachable: tuple[int, ...], driver: str = "asyncpg") -> str:
        """The URL an API process on `node` would use.

        Single-primary clusters are reached through a multi-host DSN that picks whichever
        reachable node is writable, exactly as libpq's target_session_attrs does in production.
        Everything else talks to the local node.
        """
        if not self.extra.get("single_primary"):
            return self.url(node, driver)
        order = [node, *[n for n in reachable if n != node]]
        hosts = "&".join(f"host=127.0.0.1:{self.ports[n - 1]}" for n in order)
        return (
            f"{self.scheme}+{driver}://{self.user}:{self.password}@/{self.database}"
            f"?{hosts}&target_session_attrs=read-write"
        )


CANDIDATES: dict[str, Candidate] = {
    "spock": Candidate(
        name="spock",
        scheme="postgresql",
        user="titan",
        password="spike-password",
        database="titan",
        ports=(15431, 15432, 15433),
        data_dir="/var/lib/postgresql/data",
        consensus=False,
    ),
    "patroni": Candidate(
        name="patroni",
        scheme="postgresql",
        user="titan",
        password="spike-password",
        database="titan",
        ports=(15461, 15462, 15463),
        data_dir="/var/lib/postgresql/data",
        consensus=True,
        extra={"single_primary": True},
    ),
    "yugabyte": Candidate(
        name="yugabyte",
        scheme="postgresql",
        user="yugabyte",
        password="yugabyte",
        database="yugabyte",
        ports=(15441, 15442, 15443),
        data_dir="/home/yugabyte/var",
        consensus=True,
    ),
}


def engine(
    c: Candidate,
    node: int,
    driver: str = "asyncpg",
    reachable: tuple[int, ...] | None = None,
    vector_codec: bool = True,
    **kw: Any,
) -> AsyncEngine:
    """Engine for node `node`. With `reachable`, returns what the API on that node would use."""
    connect_args: dict[str, Any] = {}
    # Server-side timeouts, so a blocked statement ends on the server instead of leaving a
    # cancelled client waiting for a cancel the server may never act on.
    if driver == "asyncpg":
        connect_args = {
            "timeout": 5,
            "command_timeout": 30,
            "server_settings": {"statement_timeout": "15000", "lock_timeout": "10000"},
        }
    elif driver == "psycopg":
        connect_args = {
            "connect_timeout": 5,
            "options": "-c statement_timeout=15000 -c lock_timeout=10000",
        }
    connect_args.update(kw.pop("connect_args", {}))
    url = c.url(node, driver) if reachable is None else c.app_url(node, reachable, driver)
    eng = create_async_engine(url, pool_pre_ping=True, pool_size=4, connect_args=connect_args, **kw)
    if driver == "asyncpg" and vector_codec:

        @event.listens_for(eng.sync_engine, "connect")
        def _register(dbapi_conn: Any, _record: Any) -> None:
            dbapi_conn.run_async(_text_vector_codec)

    return eng


async def _text_vector_codec(conn: Any) -> None:
    """Pass pgvector values through asyncpg as text.

    pgvector's SQLAlchemy type already renders vectors as text. Vanilla Postgres needs no
    codec for that, but YugabyteDB gives `vector` a fixed OID below 16384, which asyncpg
    treats as a built-in type it refuses to handle without an explicit codec.
    pgvector.asyncpg.register_vector does not help: it expects lists, not text.
    """
    schema = await conn.fetchval(
        "select n.nspname from pg_type t join pg_namespace n on n.oid = t.typnamespace "
        "where t.typname = 'vector'"
    )
    if schema:
        await conn.set_type_codec("vector", schema=schema, encoder=str, decoder=str, format="text")


def sh(*args: str, check: bool = True, timeout: float = 600) -> str:
    res = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout


def container_ip(name: str) -> str:
    data = json.loads(sh("docker", "inspect", name))
    nets = data[0]["NetworkSettings"]["Networks"]
    return next(iter(nets.values()))["IPAddress"]


class Faults:
    """Pause nodes and cut links between them without touching client access.

    Clients reach nodes through published ports, which arrive from the bridge gateway,
    so dropping only peer IPs partitions the cluster while the runner still sees every node.
    """

    def __init__(self, c: Candidate) -> None:
        self.c = c

    def _sidecar(self, node: int) -> str:
        name = f"{self.c.container(node)}-net"
        running = sh("docker", "ps", "-q", "-f", f"name=^{name}$").strip()
        if not running:
            sh("docker", "rm", "-f", name, check=False)
            sh(
                "docker", "run", "-d", "--name", name, "--net", f"container:{self.c.container(node)}",
                "--cap-add", "NET_ADMIN", NETSHOOT, "sleep", "infinity",
            )
        return name

    def _drop(self, node: int, peer: int) -> None:
        ip = container_ip(self.c.container(peer))
        side = self._sidecar(node)
        sh("docker", "exec", side, "iptables", "-A", "INPUT", "-s", ip, "-j", "DROP")
        sh("docker", "exec", side, "iptables", "-A", "OUTPUT", "-d", ip, "-j", "DROP")

    def isolate(self, node: int) -> None:
        for peer in NODES:
            if peer != node:
                self._drop(node, peer)
                self._drop(peer, node)

    def heal(self) -> None:
        for node in NODES:
            sh("docker", "exec", self._sidecar(node), "iptables", "-F")

    def pause(self, node: int) -> None:
        sh("docker", "pause", self.c.container(node))

    def unpause(self, node: int) -> None:
        sh("docker", "unpause", self.c.container(node), check=False)

    def cleanup(self) -> None:
        for node in NODES:
            self.unpause(node)
            sh("docker", "rm", "-f", f"{self.c.container(node)}-net", check=False)


async def scalar(eng: AsyncEngine, sql: str, timeout: float = 20, **params: Any) -> Any:
    async def run() -> Any:
        async with eng.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar()

    return await asyncio.wait_for(run(), timeout)


async def execute(eng: AsyncEngine, sql: str, timeout: float = 20, **params: Any) -> None:
    async def run() -> None:
        async with eng.begin() as conn:
            await conn.execute(text(sql), params)

    await asyncio.wait_for(run(), timeout)


async def attempt(coro: Awaitable[Any]) -> dict[str, Any]:
    """Run one operation and record whether it succeeded and how long it took."""
    start = time.monotonic()
    try:
        value = await coro
        return {"ok": True, "seconds": round(time.monotonic() - start, 2), "value": value}
    except Exception as exc:  # noqa: BLE001 - the failure itself is the measurement
        return {
            "ok": False,
            "seconds": round(time.monotonic() - start, 2),
            "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}",
        }


async def wait_until(
    probe: Callable[[], Awaitable[bool]], timeout: float, interval: float = 1.0
) -> float | None:
    """Poll until probe() is true. Returns elapsed seconds, or None on timeout."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            if await probe():
                return round(time.monotonic() - start, 1)
        except Exception:  # noqa: BLE001 - a node that is still catching up may refuse queries
            pass
        await asyncio.sleep(interval)
    return None


def dir_bytes(c: Candidate, node: int) -> int:
    out = sh("docker", "exec", c.container(node), "du", "-sb", c.data_dir, check=False)
    return int(out.split()[0]) if out else -1
