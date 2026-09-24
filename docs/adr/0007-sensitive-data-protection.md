# 0007. Protection of sensitive domains

- Status: **Proposed**
- Date: 2026-09-24

## Context

Health and finance entries, and some notes and memories, are sensitive. Data is
replicated to every node, including a work laptop and possibly a VPS that the
owner does not fully control. It is also sent to the Claude API when the agent
reasons about it.

## Options under evaluation

1. **Access control only**: per-user ownership checks, full-disk encryption on
   every node, and encryption in transit through Tailscale. Simple, and search
   and vectors keep working. A stolen unencrypted disk or a compromised VPS
   exposes everything.
2. **Field-level encryption** of sensitive fields with per-user keys held by the
   API. A compromised replica at rest reveals nothing. Encrypted fields cannot
   be filtered or embedded without decrypting in the application.
3. **Node placement rules**: sensitive domains are replicated only to trusted
   nodes (for example, not to the VPS). This depends on the engine chosen in
   [ADR 0006](0006-replicated-database-with-vectors.md).
4. **Agent exposure controls**: per-domain rules about what may be sent to
   Claude, such as sending aggregates only for finance.

These options can be combined.

## Decision

Pending. It will be decided together with or after ADR 0006.

## Consequences

Until this is decided, health and finance data cannot be shared between users,
and the VPS node is not used in production.
