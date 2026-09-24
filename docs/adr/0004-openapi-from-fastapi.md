# 0004. OpenAPI schema generated from FastAPI is the API contract

- Status: Accepted
- Date: 2026-09-24

## Context

Three clients (Android, CLI, Web UI) in up to three repositories use the same
API. Hand-written models on every side drift apart. Writing the contract first
by hand slows down iteration while the API is young.

## Decision

The FastAPI app is the source of truth. Its generated OpenAPI 3.1 schema is
exported to [`docs/api/openapi.json`](../api/README.md) and committed. CI fails
if the committed file differs from what the code generates. Clients generate
their API code from the committed file.

## Consequences

- Every API change in this repository comes with an updated `openapi.json` in
  the same commit.
- Client repositories update by copying or fetching a tagged version of the
  file and regenerating.
- Pydantic models must be written with the generated schema in mind: explicit
  names, no anonymous unions, documented enum values.
