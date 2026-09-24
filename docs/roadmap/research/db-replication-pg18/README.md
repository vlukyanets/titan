# Spike: replicated database on PostgreSQL 18

Follow-up to the first ADR 0006 spike, run after the owner fixed PostgreSQL 18
as the minimum and all nodes as equally trusted. It adds ORM checks, a
partition test for YugabyteDB and the PostgreSQL 18.6 specifics of Spock. The
findings are in [ADR 0006](../../../adr/0006-replicated-database-with-vectors.md);
the plan is [m0-research.md](../../plans/m0-research.md). Throwaway code:
removed from the tree once its findings are in the ADR, kept in git history.

## Layout

| Path | Contents |
|---|---|
| `images/pg/` | PostgreSQL 18 patched for Spock, with Spock 5.0.11, pgvector and Patroni |
| `candidates/<name>/` | Compose file and setup script for a three-node cluster |
| `spike/` | SQLAlchemy models, cluster helpers, fault injection, scenarios S1–S8 |
| `migrations/` | The two Alembic revisions every candidate runs |
| `results/<name>.json` | Raw results per candidate |

## Running

```bash
images/pg/build.sh                 # SPIKE_EXTRA_CA=... behind an intercepting proxy
candidates/spock/up.sh
SPIKE_URL=postgresql+psycopg://titan:spike-password@127.0.0.1:15431/titan \
  uv run alembic upgrade 0001
uv run python -m spike.run spock s5 s7 s6 s1 s3 s8 s4 s2
```

Nodes: `n1` home server, `n2` VPS, `n3` laptop. Partitions use iptables in a
`nicolaka/netshoot` sidecar that shares a node's network namespace; a sleeping
laptop is `docker pause`.
