# Plan: M2 trackers

Implements the "Trackers" item of [M2](../milestones.md) and the
[trackers spec](../../spec/domains/trackers.md). Values are plain columns,
because [ADR 0007](../../adr/0007-sensitive-data-protection.md) protects them
with the encrypted volume rather than per-field encryption.

Branch `feature/m2-trackers`, stacked on `feature/m2-push-retries`.

- `titan.domains.trackers`: `trackers` and `tracker_entries` tables, the
  templates, the service with owner-only access, stats per local period
  (computed in SQL with the owner's time zone) and streaks.
- REST API under `/api/v1/trackers`.
- Agent tools, with the exposure levels of ADR 0007, need a live agent run and
  follow once a credential is available.

Tasks:

- [x] Spec and this plan.
- [ ] Tables, migration, templates, service, stats and streaks.
- [ ] Trackers API, OpenAPI regenerated, docs updated.
- [ ] Agent tools with exposure levels (after a live agent run).
