# 0012. Browser sessions for the Web UI

- Status: Proposed
- Date: 2026-09-24

## Context

Every node serves the Web UI next to its API on one origin
([titan-web ADR 0002](https://github.com/vlukyanets/titan-web/blob/master/docs/adr/0002-served-by-the-node.md)).
Signing in to it must be an ordinary username and password form, without a
pairing step
([Web UI spec](https://github.com/vlukyanets/titan-web/blob/master/docs/spec/web.md#serving-and-signing-in)).
Today every client pairs as a device and sends its token as a bearer header
([accounts and devices](../spec/accounts.md)). Device tokens never expire, so a
token kept where page scripts can read it would be stolen for good by any
script injection. The rest of the system already relies on devices: they are
listed, revoked by users and the owner, and their revocation replicates to
every node.

## Options

1. **Pair from the browser and keep the token in `localStorage`**, sent as a
   bearer header. No backend change, but any script injection can read the
   token and use it from anywhere on the tailnet until someone notices and
   revokes it.
2. **A separate session table** with server-side sessions. The usual web
   pattern, but it duplicates what devices already do (listing, revocation,
   replication) with a second mechanism to secure and test.
3. **A device per browser, with its token in an `HttpOnly` cookie.** Signing in
   creates a `web` device like pairing does, but the token goes into a cookie
   that scripts cannot read. Revocation, the device list and replication stay
   as they are.
4. **Short-lived signed tokens with refresh tokens.** Useful when many services
   check tokens without a database. Here every request reaches a node with the
   database at hand, and revocation would get harder, not easier.

## Decision

Option 3.

- `POST /api/v1/session` takes a username and password and applies the same
  checks as pairing (lockout, equal timing and the same error for an unknown
  username). It creates a device with platform `web`, named after the browser
  and system from the `User-Agent` header (for example "Firefox on Linux"),
  and answers the user and the device id. The token is never in the body.
- The token is set as the cookie `__Host-titan_session` with `HttpOnly`,
  `Secure`, `SameSite=Strict`, `Path=/` and a `Max-Age` of 30 days. The
  `__Host-` prefix ties it to the exact node address.
- `DELETE /api/v1/session` revokes the device and clears the cookie. A `401`
  on a request that sent the cookie clears it too.
- Authentication accepts a bearer header first, then the cookie. Clients other
  than the browser keep using bearer tokens.
- **Cross-site requests.** When the cookie authenticates a request other than
  `GET`, `HEAD` or `OPTIONS`, the request must carry the header
  `X-Titan-Request: 1`, and its `Origin`, when present, must be the node's own
  origin. Otherwise the node answers `403`. A cross-site form cannot set the
  header, and a cross-site script cannot send it without a CORS preflight that
  the node never approves. `SameSite=Strict` is the first line; this is the
  second.
- **Idle expiry.** A `web` device that has not been seen for 30 days is refused
  and revoked. The node already updates `last_seen_at` at most once an hour;
  when it does, it sends the cookie again with a fresh `Max-Age`, so an active
  browser stays signed in. Devices of other platforms do not expire.
- **HTTPS.** `Secure` cookies and the browser features the UI relies on need a
  secure context, so the UI is reached over HTTPS: Tailscale terminates TLS
  with the node's `ts.net` certificate (`tailscale serve`) and forwards to the
  API on the node itself. Nothing listens outside the tailnet. For development,
  browsers treat `http://localhost` as secure.

### Hardening

- **Tokens.** A session token has 256 bits from the operating system's random
  source, like a device token, and the node stores only its SHA-256 hash.
  Every sign-in creates a new device and token, so a session cannot be fixed
  in advance, and a token never appears in a URL, a response body or a log.
- **Sign-in abuse.** Besides the per-account lockout, sign-in attempts are
  limited per client address. Sign-in also requires `X-Titan-Request` and a
  same-origin `Origin`, so another site cannot sign a browser in to an
  account of its choosing.
- **Lifetime.** Besides the 30-day idle expiry, a browser session ends 90 days
  after sign-in whatever its use. Changing a password revokes all of the
  user's `web` devices except the current one, and disabling a user ends all
  of them.
- **Recent sign-in for sensitive changes.** Changing a password, creating or
  disabling users, revoking another user's device, changing policies or
  budgets and approving `destructive` actions need a sign-in in the last
  15 minutes. Older sessions get `403` with a problem type that asks the UI
  to confirm the password, which refreshes the sign-in time without a new
  device.
- **New sign-in notice.** Every browser sign-in sends the user a notification
  naming the browser and node, so an unexpected sign-in is noticed.
- **Response headers.** The node serves the UI and the API with
  `Strict-Transport-Security`, `Content-Security-Policy: default-src 'self';
  script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none';
  frame-ancestors 'none'; base-uri 'none'; form-action 'self';
  require-trusted-types-for 'script'`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin`,
  `Cross-Origin-Resource-Policy: same-origin` and a `Permissions-Policy` that
  turns off camera, microphone, location and payment. API responses carry
  `Cache-Control: no-store`.
- **No CORS.** The node sends no `Access-Control-Allow-*` headers, so no other
  origin can read its responses.

## Consequences

- Script injection in the UI can still act as the user while the page is
  open, but it cannot carry the token away. The UI's strict
  `Content-Security-Policy` makes injection itself harder: no inline or
  foreign scripts run, and Trusted Types block HTML strings from reaching the
  DOM. Inline styles stay allowed because the component libraries inject
  them; a style injection cannot run code.
- A UI library that assigns HTML strings must be replaced, or wrapped in a
  named Trusted Types policy that sanitises its input.
- Browsers show up in the device list and are revoked like any other device.
- The cookie belongs to one node address, so a user signs in once per node
  they open in the browser, and each sign-in is its own device.
- The accounts spec, the API conventions and the OpenAPI schema gain the
  session endpoints and the cookie, and tests cover the cross-site checks, the
  idle expiry and that a bearer token and a cookie resolve the same way.
- Node setup documents `tailscale serve` for HTTPS.
