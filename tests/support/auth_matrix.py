"""Route auth classifier (MT-2, wave-6 P-210). Not a test module.

Every OpenAPI ``(METHOD, path)`` gets exactly one auth class:

* ``ADMIN``  — the route's dependant tree contains ``deps_auth.require_admin``
  (found by walking FastAPI's dependants, so router-level
  ``dependencies=[Depends(require_admin)]`` and ``admin_actor`` count).
* ``AGENT``  — agent credential only (``get_principal``); listed explicitly.
* ``EITHER`` — the handler branches on ``is_admin_bearer`` (admin acts as a
  named agent, or the agent key acts as itself); listed explicitly because
  the branch lives inside the handler and cannot be introspected.
* ``PUBLIC`` — unauthenticated by design; listed explicitly with a reason.

Anything else is ``None`` (unclassified) and fails ``tests/test_auth_matrix.py``.
``scripts/gen_api_docs.py`` uses the same classifier for docs/reference/api.md.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts

from mcprouter.api import deps_auth

ADMIN, AGENT, EITHER, PUBLIC = "ADMIN", "AGENT", "EITHER", "PUBLIC"

AGENT_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/me"): "the calling principal",
    ("GET", "/api/v1/me/approvals/{approval_id}"): "own approvals only (other agent's -> 404)",
    ("POST", "/api/v1/route"): "identity from the credential; body agent_id must match",
    ("POST", "/api/v1/decision/systemone"): "System One edge",
}
EITHER_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/tools/{tool_id}/execute"): "admin must name agentId",
    ("POST", "/api/v1/route/{request_id}/feedback"): "admin -> source=human",
    ("GET", "/api/v1/skills/bundle"): "admin must name agentId",
    ("POST", "/api/v1/skills/{skill_id}/activate"): "admin must name agentId",
    ("GET", "/api/v1/skills/{skill_id}/resources/{path}"): "admin must name agentId",
}
PUBLIC_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/healthz"): "liveness probe (D14)",
    ("GET", "/readyz"): "readiness probe (D14, E1 P-108)",
}

_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_CONVERTOR = re.compile(r"\{([^}:]+):[^}]+\}")


@dataclass(frozen=True)
class RouteInfo:
    method: str
    path: str  # OpenAPI form: {name}, convertors stripped
    endpoint: Callable[..., Any]
    summary: str
    tags: tuple[str, ...]
    calls: frozenset[Any]


def openapi_path(path: str) -> str:
    return _CONVERTOR.sub(r"{\1}", path)


def _calls(dependant: Any) -> Iterator[Any]:
    yield dependant.call
    for sub in dependant.dependencies:
        yield from _calls(sub)


def api_routes(app: FastAPI) -> list[RouteInfo]:
    """Every schema-visible APIRoute of `app`, in registration order."""
    out: list[RouteInfo] = []
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute) or not ctx.include_in_schema:
            continue
        assert ctx.path is not None and ctx.endpoint is not None  # APIRoute always has both
        doc = (ctx.endpoint.__doc__ or "").strip().splitlines()
        summary = ctx.summary or (doc[0] if doc else "")
        for method in sorted((ctx.methods or set()) & _METHODS):
            out.append(
                RouteInfo(
                    method=method,
                    path=openapi_path(ctx.path),
                    endpoint=ctx.endpoint,
                    summary=summary,
                    tags=tuple(str(t) for t in ctx.tags or ()),
                    calls=frozenset(_calls(ctx.dependant)),
                )
            )
    return out


def classify(route: RouteInfo) -> str | None:
    key = (route.method, route.path)
    explicit = [
        cls
        for cls, table in ((AGENT, AGENT_ROUTES), (EITHER, EITHER_ROUTES), (PUBLIC, PUBLIC_ROUTES))
        if key in table
    ]
    admin = deps_auth.require_admin in route.calls
    if admin:
        # An explicitly listed route must not also carry the admin gate.
        return ADMIN if not explicit else None
    return explicit[0] if len(explicit) == 1 else None


def matrix(app: FastAPI) -> dict[tuple[str, str], str | None]:
    return {(r.method, r.path): classify(r) for r in api_routes(app)}
