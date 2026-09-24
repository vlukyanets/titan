# 0013. One cluster address through Tailscale Services

- Status: Accepted
- Date: 2026-09-24

## Context

Every node serves the API and the Web UI, and every node can answer any
request because they share a replicated database
([ADR 0006](0006-replicated-database-with-vectors.md)). Clients so far reach
one node by its own tailnet name and switch nodes themselves when it fails.
For the Web UI that means a separate sign-in on every node, because a browser
keeps a cookie per address
([ADR 0012](0012-browser-sessions-for-the-web-ui.md)); the owner wants signing
in to be transparent across nodes. Tailscale Services give a group of
machines one MagicDNS name and virtual IP: several hosts advertise the same
service, and Tailscale connects each client to the nearest available host.

## Options

1. **One address for the cluster** through a Tailscale Service. One origin,
   one cookie, failover without client logic. Tailscale Services are still a
   beta feature, and service hosts must be tagged devices.
2. **A cookie for the whole tailnet domain**, with the UI switching between
   node addresses itself. Works with plain Tailscale, but every HTTPS host in
   the tailnet receives the session cookie, the API needs CORS for the other
   nodes, and a page opened on a dead node does not load at all.
3. **Tailscale identity headers instead of passwords.** Seamless, but it
   needs a Tailscale account per person, gives no identity to tagged devices
   and shared computers, and trusts a header. Kept as a possible later
   convenience, not the way in.
4. **Nodes handing sign-in tickets to each other.** Complex, and it fails
   exactly when the node that holds the session is down.

## Decision

Option 1.

- The tailnet defines the Service `svc:titan`, reachable as
  `titan.<tailnet>.ts.net`. Every node advertises it with
  `tailscale serve --service=svc:titan`, which terminates HTTPS with the
  service's certificate and forwards to the node's API on the node itself.
- Every node is a tagged device, `tag:titan-node`, as service hosts must be.
  The tailnet policy lets household devices reach `svc:titan` on port 443 and
  lets nodes reach each other for replication. Nothing else reaches the API.
- **A node advertises only while it is ready.** A supervisor on the node polls
  `GET /api/v1/health/ready` and runs `tailscale serve advertise` or
  `tailscale serve drain` from the answer. Ready means the database answers,
  the schema is at the current revision, and replication from every reachable
  peer lags less than 60 seconds. A laptop coming back after a week stays
  drained until it has caught up, so it never serves stale data or refuses
  sessions it has not received yet. A node drains before a planned shutdown
  or upgrade.
- Clients use the cluster address by default: the Web UI is opened there, the
  Android app and the CLI log in to it. A node's own address still works for
  administration and diagnostics, with its own sessions.
- Any node verifies any token, so no stickiness is needed. A brand-new session
  can reach a node a moment before its device row does; the Web UI retries a
  `401` once after two seconds when its sign-in is less than a minute old.
- A connection cut by failover (a streamed chat reply) is recovered the usual
  way: the client reloads the thread, where the node that ran the turn keeps
  writing the reply.

## Consequences

- One sign-in works on every node, and losing a node costs at most a dropped
  connection.
- Client-side failover and node lists become optional. The Android app's node
  list turns into an advanced fallback (a follow-up in titan-android).
- Tagging the nodes, the laptop included, removes their user identity in
  Tailscale: they are managed through the tailnet policy by tag.
- Deployment gets the supervisor that drives `advertise` and `drain`, and the
  readiness check gets the replication condition.
- The feature is in beta, so milestone M3 starts with a spike on a test
  tailnet: failover time, the service certificate, draining from a container,
  SSE through the service, and the plan it needs. If it fails, a new ADR
  supersedes this one with option 2.
