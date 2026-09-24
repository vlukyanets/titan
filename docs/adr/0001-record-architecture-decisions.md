# 0001. Record architecture decisions

- Status: Accepted
- Date: 2026-09-24

## Context

TITAN is developed largely by AI agents working across several repositories and
sessions. Agents and people both need to know why the system is built the way
it is, without digging through old chats or pull requests.

## Decision

Significant decisions are recorded as architecture decision records in
`docs/adr/`, one Markdown file per decision, numbered in order and based on
[`0000-template.md`](0000-template.md).

- A decision is significant if it is hard to reverse, affects more than one
  component or repository, or picks between reasonable alternatives.
- An ADR starts as **Proposed** while it is being researched. It becomes
  **Accepted** once decided.
- Accepted ADRs are not rewritten. A change of mind is a new ADR that
  supersedes the old one, and the old one gets its status updated.
- Research notes and spikes that feed an ADR live in `docs/roadmap/` and are
  deleted once the ADR is accepted.

## Consequences

Every agent session can load the relevant ADRs as context. Proposed ADRs make
open decisions visible, and the roadmap links to them.
