"""POST /api/v1/decision/systemone — this router as a System One edge.

Another MCP Router (MCPR_DECISION_BACKEND=remote) can point at this endpoint
and use our local decider (e.g. Laya). Auth: agent key or admin; per-principal
sliding-window rate limit (MCPR_DECISION_RATE_LIMIT_PER_MIN); body caps; runs
through the engine's validated, deadline-bounded decider under the inference
semaphore.

Loop guard (hop count): every outbound remote decision call carries
`X-MCPR-Decision-Hop: n+1`, and this edge refuses any request that arrives with
a hop >= 1 when serving it would forward again (our backend is `remote`), 503,
so an MCPR->MCPR chain is at most one hop and cannot loop, whatever names or
ports are involved. A malformed hop header is treated as already forwarded. Belt-and-braces: if OUR backend is
`remote` and points at this listener (same host:port or a localhost alias) we
also refuse. Residual: only a non-MCPR intermediary that strips the header can
hide a loop; the per-hop deadline still bounds it.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from mcprouter.api.deps_auth import get_principal
from mcprouter.inference import serve
from mcprouter.inference.adapters import DeadlineDecisionModel
from mcprouter.inference.errors import InferenceError
from mcprouter.inference.remote_systemone import DECISION_HOP, HOP_HEADER
from mcprouter.models import AgentPrincipal
from mcprouter.net_policy import LOOPBACK_HOSTS

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/decision", tags=["decision"])

# Loopback names (E1 P-109) plus the wildcard binds: a router bound to 0.0.0.0
# answers on loopback too, so a remote endpoint naming either is a self-loop.
_LOCAL_ALIASES = LOOPBACK_HOSTS | {"0.0.0.0", "::"}


class SystemOneRequest(BaseModel):
    model: str = Field(default="", max_length=256)
    state: str = Field(max_length=serve.MAX_STATE_CHARS)
    questions: dict[str, Any]


def _port(scheme: str, port: int | None) -> int:
    return port if port is not None else (443 if scheme == "https" else 80)


def is_self_loop(endpoint: str, request: Request) -> bool:
    try:
        ep = urlsplit(endpoint)
        ep_host, ep_port = (ep.hostname or "").lower(), _port(ep.scheme, ep.port)
    except ValueError:
        return False
    me_host = (request.url.hostname or "").lower()
    me_port = _port(request.url.scheme, request.url.port)
    if ep_port != me_port:
        return False
    return ep_host == me_host or (ep_host in _LOCAL_ALIASES and me_host in _LOCAL_ALIASES)


MAX_HOP_DIGITS = 3


def parse_hop(raw: str | None) -> int:
    """Absent -> 0. Up to 3 ASCII decimal digits -> their value. Anything else
    (``"²"`` and ``"٣"`` pass ``isdigit``/``isdecimal`` but are not ASCII; 5000
    digits exceed ``int()``'s limit; signs, spaces, dots) is malformed and
    treated as already forwarded (1) — never a 500."""
    if raw is None:
        return 0
    if raw.isascii() and raw.isdecimal() and len(raw) <= MAX_HOP_DIGITS:
        return int(raw)
    return 1


@router.post("/systemone")
def systemone(
    body: SystemOneRequest, request: Request, principal: AgentPrincipal = Depends(get_principal)
) -> dict[str, Any]:
    settings = request.app.state.settings
    if settings.decision_backend == "remote" and is_self_loop(settings.decision_endpoint, request):
        raise HTTPException(503, "decision edge refused: the remote backend points at this router")
    hop = parse_hop(request.headers.get(HOP_HEADER))
    if hop >= 1 and settings.decision_backend == "remote":
        raise HTTPException(503, "decision edge refused: request already forwarded by a router")
    DECISION_HOP.set(hop)
    # D10: the app registry's "decision" surface (pruned; one budget per principal).
    if not request.app.state.limiters.try_acquire("decision", f"principal:{principal.id}"):
        raise HTTPException(429, "decision edge rate limit exceeded; retry later")
    try:
        parsed = serve.parse_questions(body.questions)
    except serve.SystemOneRequestError as exc:
        raise HTTPException(422, str(exc)) from None
    engine = getattr(request.app.state, "inference_engine", None)
    if engine is None:
        raise HTTPException(503, "inference engine is not configured")
    model: DeadlineDecisionModel = request.app.state.decision_model
    try:
        answers = serve.answer(model, body.state, parsed, deadline_s=settings.decision_timeout_s)
    except InferenceError as exc:
        log.warning("decision edge failed: %s", type(exc).__name__)
        raise HTTPException(503, "decision backend unavailable; retry later") from None
    return {
        "model": model.name,
        "answers": answers,
        "usage": {
            "input_tokens": serve.approx_input_tokens(body.state, parsed),
            "output_tokens": 0,
        },
    }
