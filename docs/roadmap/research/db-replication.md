# Research: replicated database with vectors

Feeds [ADR 0006](../../adr/0006-replicated-database-with-vectors.md). Delete this
file once the ADR is accepted.

## Questions to answer per candidate

1. How are writes on two nodes at once resolved? Is the rule per table or per
   column?
2. What happens when the laptop is away for a week? Does it catch up by itself,
   and how much disk does the backlog use?
3. Can the scheduler fire a reminder exactly once? Test: two workers race for
   the same lease on two nodes during a network partition.
4. Vector search: which index types, with what recall and latency at 100 k
   chunks of 768–1024 dimensions?
5. Alembic: can a revision with a new column and a new vector index be applied
   on one node and reach the others? What manual steps are needed?
6. Memory and CPU at idle on a laptop.
7. Backup and point-in-time restore.
8. Licence (it must be usable for free, self-hosted).

## Candidates

- PostgreSQL 17 + pgEdge Spock + pgvector
- SQLite + cr-sqlite + sqlite-vec
- CockroachDB (built-in vector type)
- YugabyteDB YSQL + pgvector

## Spike plan

- Docker Compose with three containers standing in for the nodes, and
  `tc netem` or container pause to simulate the laptop dropping off.
- The same small schema for every candidate: `tasks`, `jobs` (leases),
  `embeddings`, created with Alembic.
- A script that runs the scenarios above and prints a comparison table.

## Findings

_None yet._
