"""Serve the built dashboard (ui/dist) from FastAPI with an SPA fallback.

Installed as the 404 handler, so every API route, /mcp and /metrics win.
Any other GET path resolves to a file inside the dist dir (path-traversal safe:
resolved, then containment-checked) or falls back to index.html. When the dist
dir is absent, `/` returns a short page explaining how to build it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

# Paths owned by the backend: never answered with index.html (a typo'd API
# call must 404 as JSON, not 200 with the SPA shell).
RESERVED_PREFIXES = ("api", "mcp", "metrics", "healthz", "docs", "openapi.json", "redoc")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
}
_IMMUTABLE = "public, max-age=31536000, immutable"
_NO_STORE = "no-store"

_NOT_BUILT = """<!doctype html><html><head><meta charset="utf-8">
<title>MCP Router</title></head><body style="font-family:sans-serif;max-width:40rem;margin:3rem auto">
<h1>MCP Router</h1><p>The API is running, but the dashboard has not been built.</p>
<pre>cd ui &amp;&amp; npm ci &amp;&amp; npm run build</pre>
<p>Then restart, or point <code>MCPR_UI_DIST</code> at a built <code>dist</code> directory.
For UI development use <code>npm run dev</code> (proxies to this server).</p>
</body></html>"""


def _reserved(path: str) -> bool:
    head = path.split("/", 1)[0]
    return head in RESERVED_PREFIXES


def _resolve(dist: Path, path: str) -> Path | None:
    """A regular file inside `dist`, or None. Rejects traversal and symlink escape."""
    if not path or "\x00" in path:
        return None
    try:
        candidate = (dist / path).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(dist) or not candidate.is_file():
        return None
    return candidate


def mount_ui(app: FastAPI, dist_dir: str) -> None:
    """Install the SPA as the 404 fallback, not as a catch-all route.

    A `/{path:path}` route would partially match every non-GET request and turn
    Starlette's no-match handling (404, trailing-slash redirect for /mcp/) into
    405s. As a 404 handler it only runs when no route/mount matched at all.
    """
    dist = Path(dist_dir).resolve()
    index = dist / "index.html"

    async def spa(request: Request, exc: Exception) -> Response:
        path = request.url.path.lstrip("/")
        if request.method not in ("GET", "HEAD") or _reserved(path):
            assert isinstance(exc, StarletteHTTPException)
            return await http_exception_handler(request, exc)
        if not index.is_file():
            return HTMLResponse(
                _NOT_BUILT, headers={**SECURITY_HEADERS, "Cache-Control": _NO_STORE}
            )
        found = _resolve(dist, path)
        if found is not None and found != index:
            immutable = found.parent == dist / "assets"
            cache = _IMMUTABLE if immutable else "no-cache"
            return FileResponse(found, headers={**SECURITY_HEADERS, "Cache-Control": cache})
        return FileResponse(index, headers={**SECURITY_HEADERS, "Cache-Control": _NO_STORE})

    app.add_exception_handler(404, spa)


__all__ = ["RESERVED_PREFIXES", "SECURITY_HEADERS", "mount_ui"]
