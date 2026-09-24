# Plan: M3 serving the Web UI

Implements the node's side of
[titan-web ADR 0002](https://github.com/vlukyanets/titan-web/blob/master/docs/adr/0002-served-by-the-node.md)
and the response headers of [ADR 0012](../../adr/0012-browser-sessions-for-the-web-ui.md),
part of the "Web UI" item of [M3](../milestones.md). Browser sign-in (the
session endpoints and the cookie) is a separate branch.

Two branches:

- `feature/m3-web-ui-serving`, stacked on `spec/m3-web-foundation-docs`: the
  headers, the setting and the static routes.
- `feature/m3-web-ui-image`, on top of it: the pinned release in the image. Its
  pull request opens once titan-web `v0.1.0` is published, because the image
  build downloads that release and fails until it exists.

## Decisions

- **`TITAN_WEB_UI_DIR`** names a directory holding a UI build (`index.html`
  and `assets/`). Unset, the node serves only the API, as before. Set to a
  directory without `index.html`, `titan-api` refuses to start, so a broken
  image or mount shows at once. The image sets it to its pinned release;
  for development it can point at a local `titan-web/dist`.
- **Routes**, after every API router and outside the OpenAPI schema:
  - `/api` and everything below it stay the API; unknown paths there answer
    the usual `404` problem.
  - `/assets/<file>` serves a content-hashed file with
    `Cache-Control: public, max-age=31536000, immutable`, or `404`. A missing
    asset never falls back to `index.html`, so a stale page fails loudly
    instead of parsing HTML as a script.
  - Any other `GET` or `HEAD` serves the file of that name from the root of
    the build when there is one (such as `favicon.svg`), otherwise
    `index.html`, both with `Cache-Control: no-cache`, so the UI's router
    handles deep links. Other methods answer `405`.
  - Paths are resolved inside the build directory; anything that would leave
    it, or names a hidden file, is treated as not found.
- **Headers** come from one pure ASGI middleware, so streamed chat replies
  are not buffered. Every response gets the ADR 0012 set:
  `Strict-Transport-Security`, the `Content-Security-Policy` with Trusted
  Types, `X-Content-Type-Options`, `Referrer-Policy`,
  `Cross-Origin-Opener-Policy`, `Cross-Origin-Resource-Policy` and
  `Permissions-Policy`. API responses also get `Cache-Control: no-store`.
- **No interactive API docs.** FastAPI's `/docs` and `/redoc` load scripts
  from a CDN, which the CSP forbids and the security rules do not allow on a
  node. The committed `docs/api/openapi.json` is the reference.
- **The pin** is `web-ui.json`: a titan-web version and the SHA-256 of its
  release archive. A build stage runs `scripts/fetch_web_ui.py`, which
  downloads the archive from the titan-web GitHub release, refuses it unless
  the hash matches, and extracts it with Python's `data` filter (no absolute
  paths, links out of the directory or special files). Upgrading the UI is a
  commit that changes the pin. titan-web builds its archive reproducibly, so
  the hash of a tagged commit is known before the release exists.

## Tasks

- [x] This plan.
- [ ] Security headers middleware; `/docs` and `/redoc` removed.
- [ ] `TITAN_WEB_UI_DIR` and the static routes.
- [ ] Architecture, API and agent docs updated; milestone updated.
- [ ] titan-web `v0.1.0` published.
- [ ] `web-ui.json`, the fetch script, the image stage and their docs
      (`feature/m3-web-ui-image`).
