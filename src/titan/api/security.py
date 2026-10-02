"""Security headers on every response (ADR 0012, "Response headers").

A pure ASGI middleware rather than Starlette's BaseHTTPMiddleware, so streamed
responses such as chat replies pass through without being buffered.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        # Component libraries of the Web UI set inline styles; a style cannot run code.
        "style-src 'self' 'unsafe-inline'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "require-trusted-types-for 'script'",
    )
)

SECURITY_HEADERS: dict[str, str] = {
    # Browsers honour it only over HTTPS, which Tailscale terminates in front of
    # the node (ADR 0013). No includeSubDomains: other tailnet names are not ours.
    "Strict-Transport-Security": "max-age=31536000",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}

API_CACHE_CONTROL = "no-store"


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        api = is_api_path(scope["path"])

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                if api:
                    headers["Cache-Control"] = API_CACHE_CONTROL
            await send(message)

        await self.app(scope, receive, send_with_headers)
