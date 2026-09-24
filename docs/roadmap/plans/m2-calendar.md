# Plan: M2 calendar

Implements the calendar part of the "Calendar and planner" item of
[M2](../milestones.md) and the [calendar spec](../../spec/domains/calendar.md).

Branch `feature/m2-calendar`, stacked on `feature/m2-scheduler`.

- `titan.domains.calendar`: `events`, `event_attendees` and `planning_prefs`
  tables; ownership and attendee visibility in the service; recurring events
  expanded in their own time zone with `zoneinfo` (`tzdata` shipped as a
  dependency, so slim images have the zone database).
- REST API under `/api/v1/calendar`.
- The planner (daily plan, replanning) needs a live agent run and follows once
  a credential is available. Tasks and reminders adopt the owner's time zone
  for their repeats in a later change.

Tasks:

- [x] Spec (time zones answer open question 8) and this plan.
- [ ] Tables, migration, service, occurrence expansion and busy intervals.
- [ ] Calendar API, OpenAPI regenerated, docs updated.
- [ ] Agent tools and the planner (after a live agent run).
- [ ] Tasks and reminders repeat in the owner's time zone.
