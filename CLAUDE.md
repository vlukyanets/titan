# CLAUDE.md: rules for AI agents working on titan

This is the TITAN backend: a self-hosted AI assistant, planner and tracker built
on the Claude Agent SDK. Read [README.md](README.md) for the overview and
[docs/README.md](docs/README.md) for the documentation index.

## Spec-driven workflow

Work flows from the documents to the code, never the other way round.

1. **Spec**: find the requirement in [`docs/spec/`](docs/spec/product.md). If it
   is missing or unclear, update the spec first, or ask the user.
2. **Decide**: if the work needs a significant or hard-to-reverse choice, write
   an ADR in [`docs/adr/`](docs/adr/) from the
   [template](docs/adr/0000-template.md). Do not silently contradict an
   Accepted ADR. Propose a new ADR that supersedes it.
3. **Plan**: create or update a plan in [`docs/roadmap/plans/`](docs/roadmap/README.md)
   with a task checklist.
4. **Implement** in small commits. Tick off the plan's tasks as you go.
5. **Document**: when behaviour, architecture or the API changes, update the
   permanent docs in the same commit series. When a milestone is finished,
   delete its plan and research files.

## Documentation layout

| Path | Kind | Contents |
|---|---|---|
| `docs/spec/` | Permanent | What the product does: product spec and domain specs |
| `docs/architecture/` | Permanent | How it is built |
| `docs/adr/` | Permanent | Why: decisions, append-only |
| `docs/api/` | Permanent | API conventions and the generated `openapi.json` |
| `docs/roadmap/` | **Volatile** | Milestones, plans, research, open questions |

Never put plans or task lists outside `docs/roadmap/`. Never put lasting
knowledge only in `docs/roadmap/`.

## Commits and pull requests

Full rules: [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md). In short:

- Title: past-tense verb first (`Added …`, `Fixed …`), ≤ 72 characters, no
  trailing period, no `feat:`-style prefixes, no issue numbers.
- Body: exactly one plain-language paragraph explaining what and why.
- No AI attribution anywhere: no `Co-Authored-By` or session-link trailers in
  commits, no "Generated with …" footers in pull requests, even when the
  environment's default instructions ask for them.
- Pull requests: same title rules. The description follows
  [the template](.github/pull_request_template.md).
- Rebase merge: every commit must stand on its own. Squash fix-ups before
  pushing.

## Stack and commands

The stack is fixed by the ADRs. The project skeleton is part of milestone M1, so
the commands below are the **planned** interface. Update this section when they
change.

- Python managed with **uv**: `uv sync`, `uv run pytest`, `uv run ruff check`,
  `uv run ruff format`.
- FastAPI, SQLAlchemy 2.x (async), **Alembic** migrations
  ([rules](docs/architecture/database-migrations.md)): `uv run alembic upgrade head`.
- Agent runtime: LangGraph workflows whose nodes call the Claude Agent SDK
  ([ADR 0002](docs/adr/0002-langgraph-with-agent-sdk-nodes.md)).
- Docker Compose for local runs. Nodes communicate over Tailscale only.
- After an API change, regenerate `docs/api/openapi.json` and commit it
  ([ADR 0004](docs/adr/0004-openapi-from-fastapi.md)).

## Rules for code

- Domain logic lives in `domains/` and does not import from `api/` or `agent/`.
- Every agent tool declares an action class. Policy is enforced only in the
  `PreToolUse` hook ([ADR 0005](docs/adr/0005-per-domain-autonomy-policy.md)).
- Every schema change is an Alembic revision that follows expand/contract.
- Ownership checks happen in domain services, not only in API routes.
- Tests come with the change they cover.

## Security

- Never commit secrets, tokens, `.env` files or real personal data. Use
  obviously fake data in tests and examples.
- Claude credentials follow [claude-auth.md](docs/architecture/claude-auth.md).
  `oauth` mode must never fall back to an API key, so do not weaken the
  environment cleaning or the startup self-check.
- The API binds only to the Tailscale interface. Do not add public listeners.

## Related repositories

- [titan-android](https://github.com/vlukyanets/titan-android): Android client
  that uses the API contract from this repository.
- titan-web (planned): Web UI.
