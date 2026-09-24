# 0007. Protection of sensitive domains

- Status: Accepted
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
   nodes (for example, not to the VPS). Spock, chosen in
   [ADR 0006](0006-replicated-database-with-vectors.md), supports this with
   replication sets: each subscription lists the sets it receives
   (`spock.repset_create`, `spock.sub_create(…, replication_sets)`).
4. **Agent exposure controls**: per-domain rules about what may be sent to
   Claude, such as sending aggregates only for finance.

These options can be combined.

## Evaluation

| | Protects a stolen disk | Protects a compromised VPS | Protects data sent to Claude | Cost |
|---|---|---|---|---|
| 1 Access control + disk encryption | Yes, if the disk is encrypted | No | No | Low |
| 2 Field-level encryption | Yes | Only if keys never reach the VPS | No | High: no SQL filtering or embeddings on encrypted fields, key management, key loss means data loss |
| 3 Placement by replication set | No | Yes, for the excluded tables | No | Low: a second replication set and a subscription rule per node |
| 4 Agent exposure controls | No | No | Yes | Medium: tool variants that return aggregates |

Field-level encryption costs the most and protects against little that 1 and 3
together don't already cover. The laptop and the home server are trusted
anyway, because they hold the keys. The VPS is the node the owner controls
least, and placement keeps sensitive tables off it entirely.

## Decision

Options 1, 3 and 4 together.
Option 2 is rejected for v1.

1. **Baseline on every node**: full-disk encryption (LUKS, FileVault or
   BitLocker), traffic only over Tailscale, backups encrypted before they
   leave the node.
2. **Two replication sets**:
   - `general` holds every table except the sensitive ones and goes to all
     nodes.
   - `sensitive` holds tracker entries of kind `health` and `finance` (stored
     in their own tables), agent memories, and the embeddings of sensitive
     items. It goes only to nodes the owner marks as trusted. By default that
     is the home server and the laptop.

   Spock replicates DDL to every node, so an untrusted node has the sensitive
   tables but they stay empty. Its API answers requests for those domains with
   "not available on this node", and clients switch to another node.
3. **Exposure to Claude**: each domain has an exposure level, `full` or
   `aggregates`. With `aggregates`, agent tools return sums, averages and
   streaks instead of individual entries. Defaults: `full` in a user's own chat
   for their own data, and `aggregates` for health and finance in scheduled
   workflows (daily plan, weekly review). Users can change both per domain.

## Consequences

- Health and finance entries live in their own tables so they can belong to
  the `sensitive` set ([trackers spec](../spec/domains/trackers.md)).
- Every node has a `trusted` flag in the cluster configuration. Setup refuses
  to subscribe an untrusted node to the `sensitive` set.
- A user whose only reachable node is the VPS cannot see health, finance or
  memories until a trusted node is back.
- Sharing health and finance data between users stays disabled in v1.
