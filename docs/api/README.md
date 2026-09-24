# API contract

Decision: [ADR 0004](../adr/0004-openapi-from-fastapi.md).

- The backend is the source of truth. The FastAPI app generates the OpenAPI 3.1
  schema from its routers and Pydantic models.
- The schema is exported to [`openapi.json`](openapi.json) with
  `uv run titan openapi` and committed with every API change.
- A test regenerates the schema and fails if the committed file differs, so it
  can never drift from the code.
- Clients generate their code from the committed file:
  - [titan-android](https://github.com/vlukyanets/titan-android) generates its
    Kotlin client at build time.
  - titan-web (planned) generates a TypeScript client.
- Streaming endpoints (chat over SSE) are described in the schema as
  `text/event-stream`. Their event types are documented as schema components.

## Conventions

- Base path `/api/v1`. Breaking changes need a new version prefix.
- Authentication: `Authorization: Bearer <device token>`.
- Timestamps are RFC 3339 in UTC. Recurrence rules are RFC 5545 RRULE strings.
- Errors use RFC 9457 problem details (`application/problem+json`).
- `GET /api/v1/health` (process is up) and `GET /api/v1/health/ready` (database
  reachable, current Alembic revision) are the only unauthenticated endpoints.
  Container health checks use them.
