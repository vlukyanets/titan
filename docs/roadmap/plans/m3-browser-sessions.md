# Plan: M3 browser sessions

Implements the browser sign-in of
[ADR 0012](../../adr/0012-browser-sessions-for-the-web-ui.md) and the
[accounts spec](../../spec/accounts.md#flows), part of the "Web UI" item of
[M3](../milestones.md). The response headers and the static routes are done
([plan](m3-web-ui-serving.md)).

Two branches, stacked on `feature/m2-exposure-settings`, whose migration
comes first:

1. `feature/m3-browser-sessions`: signing in and out, the cookie, the
   cross-site checks, expiry, the sign-in limit and the new sign-in notice.
2. `feature/m3-recent-sign-in`: the password confirmation that sensitive
   changes ask for when the sign-in is older than 15 minutes.

## Decisions

- **Signing in** is `POST /api/v1/session` with username and password. It
  goes through the same `authenticate` as pairing (lockout, equal timing, one
  error), then creates a `web` device named from the `User-Agent` ("Firefox on
  Linux") and sets the cookie. The body is the user and the device id.
- **The cookie** is `__Host-TSID`: the device token, `HttpOnly`, `Secure`,
  `SameSite=Strict`, `Path=/`, `Max-Age` 30 days. Whenever `last_seen_at`
  moves (at most hourly), the response sets it again with a fresh `Max-Age`.
- **Authentication** takes the bearer header first, then the cookie. The
  cookie resolves only `web` devices. A `401` for a request that sent the
  cookie clears it.
- **Cross-site checks.** A cookie-authenticated request other than `GET`,
  `HEAD` or `OPTIONS`, and every sign-in, needs `X-Titan-Request: 1`, and an
  `Origin`, when sent, whose host is the request's `Host`. Otherwise `403`.
- **Expiry.** A `web` device last seen more than 30 days ago, or signed in
  more than 90 days ago, is revoked on its next use and answers `401`.
- **Signing out** is `DELETE /api/v1/session`: it revokes the calling device
  and clears the cookie.
- **Sign-in limit**: 10 attempts a minute per client address, counted in the
  node's memory; more answer `429`. Behind `tailscale serve` every client
  arrives from loopback, so there it is a limit for the whole node, which is
  still plenty for a household.
- **New sign-in notice**: a `system` notification "New sign-in" naming the
  browser and the node.

## Tasks

- [x] This plan.
- [x] `feature/m3-browser-sessions`: the session endpoints, cookie
      authentication, cross-site checks, expiry, the limit, the notice, tests,
      OpenAPI and docs.
- [x] `feature/m3-recent-sign-in`: sign-in time on devices, the `403` problem
      type, password confirmation, the sensitive endpoints, tests and docs.
