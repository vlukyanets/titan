# 0007. Protection of sensitive domains

- Status: **Proposed**
- Date: 2026-09-24

## Context

Health and finance entries, and some notes and memories, are sensitive. Data is
replicated to every node, including a work laptop and possibly a VPS that the
owner does not fully control. It is also sent to the Claude API when the agent
reasons about it.

All nodes are **equally trusted**: every node holds a full replica and there
are no per-node placement rules. A laptop can be stolen and a VPS runs on
someone else's hardware, so protection is designed as if any single node could
be compromised at rest.

## Options under evaluation

1. **Access control only**: per-user ownership checks, full-disk encryption on
   every node, and encryption in transit through Tailscale. Simple, and search
   and vectors keep working. A stolen unencrypted disk or a compromised VPS
   exposes everything.
2. **Field-level encryption** of sensitive fields with per-user keys held by the
   API. A compromised replica at rest reveals nothing. Encrypted fields cannot
   be filtered or embedded without decrypting in the application.
3. **Agent exposure controls**: per-domain rules about what may be sent to
   Claude, such as sending aggregates only for finance.

These options can be combined.

## Decision

Pending. It will be decided together with or after ADR 0006.

## Consequences

Until this is decided, health and finance data cannot be shared between users.
Node placement rules (replicating sensitive domains only to some nodes) were
considered and dropped, because all nodes are equally trusted.
