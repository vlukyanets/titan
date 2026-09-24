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

## Evaluation

What each option protects against, when every node is equally trusted and so
every node runs `titan-api` and `titan-worker` with everything they need to
serve any user:

| | Stolen powered-off laptop or disk image | Compromised running node (VPS host, malware) | Data sent to Claude | Cost |
|---|---|---|---|---|
| 1 Access control + full-disk encryption + encrypted backups | Yes | No | No | Low: a node setup requirement |
| 2 Field-level encryption, keys on every node | Yes (already covered by 1) | No: the running API holds the keys | No | High: no SQL sums or filters on encrypted values, no embeddings, key rotation and loss |
| 2b Field-level encryption, keys unlocked per user session | Yes | Partly: only for users not logged in | No | Very high: scheduled workflows (daily plan, stats, reminders about finance) cannot read the data unattended |
| 3 Agent exposure controls | No | No | Yes | Medium: tool variants that return aggregates |

Equal trust removes what field-level encryption is usually for. Keys cannot be
kept off some nodes, so a compromised running node reads the data whether it
is encrypted or not, and a stolen disk is already covered by full-disk
encryption. What remains of option 2 is its cost: tracker statistics would have
to be computed in the application, and sensitive notes could not be searched
semantically.

## Recommendation

Options 1 and 3 together; option 2 is rejected for v1. The owner decides; the
status stays Proposed until then.

1. **Baseline on every node**, checked in the node setup guide:
   - full-disk encryption (LUKS, FileVault or BitLocker), including the
     database volume;
   - traffic only over Tailscale;
   - backups encrypted before they leave the node;
   - no user content in logs (already a rule of the code).
2. **Exposure to Claude**, per user and per domain, with two levels:
   - `full`: agent tools return individual entries;
   - `aggregates`: agent tools return sums, averages and streaks only.

   Defaults: `full` in a user's own chat, `aggregates` for health and finance
   in scheduled workflows (daily plan, weekly review). Users change both in
   their settings.
3. Health and finance entries stay private to their owner in v1.

## Decision

Pending: the owner accepts the recommendation or picks another option.

## Consequences

If the recommendation is accepted:

- Tracker entries, notes and memories are stored as plain columns, so SQL
  aggregates and embeddings work.
- The node setup guide lists the baseline, and every node is set up to it.
- Agent tools of sensitive domains take the exposure level into account.
- Field-level encryption needs a new ADR, together with a reason to trust some
  nodes less than others.

Until this is decided, health and finance data cannot be shared between users.
Node placement rules (replicating sensitive domains only to some nodes) were
considered and dropped, because all nodes are equally trusted.
