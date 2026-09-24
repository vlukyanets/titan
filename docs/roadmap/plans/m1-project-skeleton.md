# Plan: M1 project skeleton

Covers the first M1 items in [milestones.md](../milestones.md): project layout,
tooling, CI, database access with Alembic, the API shell and the OpenAPI export.
Features (accounts, chat, agent runtime) come in their own plans.

## Decisions

- **Python 3.12 only** (`>=3.12,<3.13`), locally and in CI.
- **src layout** (`src/titan/`), one package with several entry points
  (`titan`, `titan-api`; `titan-worker` arrives with the scheduler).
- **mypy strict** with the pydantic plugin; **ruff** for format and lint;
  **import-linter** enforces that `titan.domains` never imports `titan.api` or
  `titan.agent`.
- **psycopg 3 (async)** as the database driver. The ADR 0006 spike showed it
  and asyncpg both work unchanged with Spock; psycopg also gives libpq
  multi-host connection strings for the write-affinity rule.
- **pgEdge Postgres 18 with Spock** (`ghcr.io/pgedge/pgedge-postgres`, pinned
  tag) for development and CI, the same image as the cluster in
  [ADR 0006](../../adr/0006-replicated-database-with-vectors.md), without
  subscriptions. pgvector ships in that image and is enabled when the first
  feature needs vectors.
- The pgEdge image keeps PostgreSQL's default `listen_addresses = 'localhost'`
  and initialises `SQL_ASCII` databases. Compose and CI therefore start it with
  `-c listen_addresses=*` and `POSTGRES_INITDB_ARGS="--encoding=UTF8 --locale=C"`,
  and CI starts it in a step because service containers cannot pass server
  arguments.
- **No baseline revision yet.** The first domain table brings the first
  revision. Extensions are created per node, not in revisions, as the ADR's
  Spock rules require.
- Compose publishes the API only on `TITAN_TAILSCALE_IP`, which has no default.
  Outside a container, `titan-api` refuses a wildcard bind address.

## Tasks

- [x] `pyproject.toml`, `uv.lock`, ruff, mypy, import-linter, pytest.
- [x] Settings from `TITAN_*` variables; database URL kept secret.
- [x] FastAPI app factory, RFC 9457 problem details, health and readiness.
- [x] SQLAlchemy base with a naming convention, engine and session factories.
- [x] Alembic environment (async, URL from settings), no revisions yet.
- [x] `titan migrate` under an advisory lock and `titan openapi`.
- [x] Tests: health, settings, CLI, migration round trip with drift check,
      OpenAPI drift check.
- [x] `docs/api/openapi.json` generated and committed.
- [x] Dockerfile (non-root) and Compose (pgEdge Postgres 18, migrate, API).
- [x] GitHub Actions: lint, tests on Python 3.12 with pgEdge Postgres 18,
      image build.
- [x] CLAUDE.md commands, README, API and migration docs updated.
