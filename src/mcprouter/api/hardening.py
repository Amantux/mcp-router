"""App-wide HTTP hardening (wave-6 E1, D2).

``install`` is called LAST in ``create_app`` so what it adds is the outermost
middleware: the ``HostGuard`` sees every request (API, /metrics, /docs, /mcp,
the SPA, /healthz) before routing, auth or body parsing.

* HostGuard: the ``Host`` header (port ignored) must be loopback or listed in
  ``MCPR_ALLOWED_HOSTS``; otherwise **421** JSON. This is the DNS-rebinding
  defence for the whole app (a rebound name still sends its own Host). The MCP
  SDK's own Host/Origin check on /mcp stays as defence in depth. No path is
  exempt; a missing Host header is refused too (fail closed).
* Security headers on every response: ``nosniff``, ``frame-ancestors 'none'``,
  ``no-referrer``; ``no-store`` on /api/ responses; a ``default-src 'self'``
  CSP on SPA HTML (Swagger UI at /docs loads its assets from a CDN, so it
  keeps the frame-ancestors-only policy).
"""

from __future__ import annotations

import hmac
import json
import re

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcprouter.auth import hash_key
from mcprouter.net_policy import LOOPBACK_HOSTS
from mcprouter.settings import Settings

# Port-insensitive loopback names: the one shared set (net_policy).
LOOPBACK_HOSTNAMES = LOOPBACK_HOSTS

# Test-only seam (D2): tests/support/settings.py patches this to admit the
# TestClient's "testserver". Never set from env.
_EXTRA_HOSTS: frozenset[str] = frozenset()

_SPA_CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
_FRAME_CSP = "frame-ancestors 'none'"
_DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})
_MISDIRECTED = json.dumps(
    {"detail": "Misdirected request: Host not allowed (add it to MCPR_ALLOWED_HOSTS)"}
).encode()


_HOST_RE = re.compile(r"(\[[0-9a-f:.]+\]|[^:\[\]@/\s]+)(?::\d{1,5})?")


def hostname_of(host_header: str) -> str:
    """``Host`` header -> lower-case hostname without port. ``[::1]:8400`` ->
    ``[::1]``; ``example.com:80`` -> ``example.com``. Anything that is not
    strictly ``host[:digits]`` returns "" (never allowed)."""
    m = _HOST_RE.fullmatch(host_header.strip().lower())
    return m.group(1) if m else ""


def allowed_hostnames(settings: Settings) -> frozenset[str]:
    """Loopback ∪ MCPR_ALLOWED_HOSTS (port parts dropped: D2 is port-insensitive)."""
    return LOOPBACK_HOSTNAMES | {hostname_of(h) for h in settings.allowed_hosts}


class HostGuard:
    """Pure ASGI (no BaseHTTPMiddleware: streaming /mcp responses pass through)."""

    def __init__(self, app: ASGIApp, allowed: frozenset[str]) -> None:
        self.app = app
        self.allowed = allowed

    def _host_ok(self, scope: Scope) -> bool:
        for key, value in scope.get("headers", ()):
            if key == b"host":
                name = hostname_of(value.decode("latin-1"))
                return name in self.allowed or name in _EXTRA_HOSTS
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if not self._host_ok(scope):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await send(
                {
                    "type": "http.response.start",
                    "status": 421,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_MISDIRECTED)).encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _MISDIRECTED})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "no-referrer")
                is_html = headers.get("content-type", "").startswith("text/html")
                if is_html and path not in _DOCS_PATHS:
                    headers["Content-Security-Policy"] = _SPA_CSP
                else:
                    headers.setdefault("Content-Security-Policy", _FRAME_CSP)
                if path.startswith("/api/"):
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


_UNAUTHORIZED = json.dumps({"detail": "admin token required"}).encode()


class MetricsGate:
    """D14: /metrics requires `Authorization: Bearer <MCPR_ADMIN_TOKEN>` when
    an admin token is configured (hash compared in constant time); open
    otherwise (dev mode / loopback-only posture)."""

    def __init__(self, app: ASGIApp, admin_token_hash: str | None) -> None:
        self.app = app
        self.admin_token_hash = admin_token_hash

    def _authorized(self, scope: Scope) -> bool:
        if self.admin_token_hash is None:
            return True
        for key, value in scope.get("headers", ()):
            if key == b"authorization":
                scheme, _, token = value.decode("latin-1").partition(" ")
                if scheme.lower() == "bearer" and token.strip():
                    return hmac.compare_digest(hash_key(token.strip()), self.admin_token_hash)
        return False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_UNAUTHORIZED)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _UNAUTHORIZED})


def install(app: FastAPI, settings: Settings) -> None:
    """Add the HostGuard as the outermost middleware (call LAST)."""
    app.add_middleware(HostGuard, allowed=allowed_hostnames(settings))
