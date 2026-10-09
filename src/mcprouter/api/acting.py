"""Who a REST request acts as (wave-6 P-206, D10): the ONE implementation of
the act-as-agent rules shared by execute and the skills agent routes.

* **Admin token** -> MUST name ``agentId`` (400 otherwise); runs as THAT
  principal under its own policy (unknown -> 404, disabled -> 403), audited
  ``initiated_by='admin'``. The admin token never widens what the call may do.
* **Agent key** (or the synthetic ``dev`` principal in dev mode) -> runs as
  that principal; naming a DIFFERENT agent is 403. A bad credential is 401.

``admin_actor`` is the management-API admin gate that also names the actor
for audit lines (``admin`` with a token, ``local-dev`` in dev mode).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import anyio.to_thread
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from mcprouter.api.deps_auth import get_principal, require_admin, security_of
from mcprouter.auth import is_admin_bearer
from mcprouter.execution.manager import INITIATED_BY_ADMIN
from mcprouter.models import AgentPrincipal

ADMIN_NEEDS_AGENT = (
    "Admin requests must name the agent to act as: pass agentId. "
    "The request runs under that agent's routing and policy."
)
UNKNOWN_AGENT = "Unknown agent."
AGENT_DISABLED = "Agent is disabled."
AGENT_ONLY_SELF = "An agent key can only act as its own agent."

DEV_ACTOR = "local-dev"
ADMIN_ACTOR = "admin"


@dataclass(frozen=True)
class ActingAgent:
    principal: AgentPrincipal  # detached (expunged) row, or the synthetic dev principal
    initiated_by: str | None  # INITIATED_BY_ADMIN for admin impersonation, else None

    @property
    def agent_id(self) -> str:
        return self.principal.agent_id

    @property
    def is_admin(self) -> bool:
        return self.initiated_by == INITIATED_BY_ADMIN


def principal_row(request: Request, agent_id: str) -> AgentPrincipal | None:
    """The persisted principal named `agent_id`, detached, or None."""
    _, factory = security_of(request)
    with factory() as s:
        row = s.scalars(
            select(AgentPrincipal).where(AgentPrincipal.agent_id == agent_id)
        ).one_or_none()
        if row is not None:
            s.expunge(row)
        return row


async def act_as_agent(request: Request, named: str | None) -> ActingAgent:
    """Resolve the acting agent per the rules in the module docstring."""
    config, _ = security_of(request)
    if is_admin_bearer(config, request.headers.get("authorization")):
        if not named:
            raise HTTPException(status_code=400, detail=ADMIN_NEEDS_AGENT)
        row = await anyio.to_thread.run_sync(principal_row, request, named)
        if row is None:
            raise HTTPException(status_code=404, detail=UNKNOWN_AGENT)
        if not row.enabled:
            raise HTTPException(status_code=403, detail=AGENT_DISABLED)
        return ActingAgent(row, INITIATED_BY_ADMIN)
    principal = await anyio.to_thread.run_sync(get_principal, request)  # 401 on a bad key
    if named and named != principal.agent_id:
        raise HTTPException(status_code=403, detail=AGENT_ONLY_SELF)
    return ActingAgent(principal, None)


def admin_actor(request: Request, _gate: Annotated[None, Depends(require_admin)]) -> str:
    """Admin gate (401/403/503 via ``require_admin``, a declared sub-dependency
    so route introspection sees it) that returns the audit actor name."""
    security = request.app.state.security
    return ADMIN_ACTOR if security.admin_token_hash is not None else DEV_ACTOR
