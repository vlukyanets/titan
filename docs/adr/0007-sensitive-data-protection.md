# 0007. Protection of sensitive domains

- Status: Accepted
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

1. **Access control and an encrypted Docker volume**: per-user ownership
   checks, all of TITAN's data inside an encrypted volume on every node, and
   encryption in transit through Tailscale. Simple, and search
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
| 1 Access control + encrypted Docker volume + encrypted backups | Yes | No | No | Low: a node setup requirement |
| 2 Field-level encryption, keys on every node | Yes (already covered by 1) | No: the running API holds the keys | No | High: no SQL sums or filters on encrypted values, no embeddings, key rotation and loss |
| 2b Field-level encryption, keys unlocked per user session | Yes | Partly: only for users not logged in | No | Very high: scheduled workflows (daily plan, stats, reminders about finance) cannot read the data unattended |
| 3 Agent exposure controls | No | No | Yes | Medium: tool variants that return aggregates |

Equal trust removes what field-level encryption is usually for. Keys cannot be
kept off some nodes, so a compromised running node reads the data whether it
is encrypted or not, and a stolen disk is already covered by the encrypted
volume. What remains of option 2 is its cost: tracker statistics would have
to be computed in the application, and sensitive notes could not be searched
semantically.

## Decision

Options 1 and 3 together. Option 2 is rejected for v1.

1. **Baseline on every node**, set up and checked as the
   [node setup guide](../architecture/node-setup.md) describes:
   - all of TITAN's data lives inside an encrypted volume that holds Docker's
     whole data root: the database, the ntfy cache, container logs and image
     layers. The rest of the system disk need not be encrypted, but swap
     must be, because database pages can be written to it;
   - on Linux the volume is LUKS2, unlocked by:
     - **TPM2 and Tang together** on the home server, so it comes back by
       itself after a power cut but only on the home network;
     - **TPM2 with a PIN** on a laptop;
     - **Tang over Tailscale** on a VPS, whose TPM belongs to the hoster;
   - on macOS the Docker Desktop disk inside FileVault, on Windows the WSL2
     disk on a BitLocker drive;
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

## Consequences

- Tracker entries, notes and memories are stored as plain columns, so SQL
  aggregates and embeddings work.
- Every node is set up to the [node setup guide](../architecture/node-setup.md).
- A home server stolen whole does not unlock away from the home network,
  and a laptop does not unlock without its PIN. A running node, and the
  memory of a running VPS, are not protected at rest by any of this.
- The home network needs a Tang server (on the router or a small always-on
  device); if it is down, the home server waits at boot until it is back or
  someone enters the recovery key.
- Agent tools of sensitive domains take the exposure level into account.
- Field-level encryption needs a new ADR, together with a reason to trust some
  nodes less than others.

Node placement rules (replicating sensitive domains only to some nodes) were
considered and dropped, because all nodes are equally trusted.
