"""SecurityConfig and dev-mode detection (P-607 move from api/deps_auth.py).

Dev mode (no auth) applies only when nothing at all is configured: empty
`settings.agent_keys`, no admin token, and zero principals in the DB. It
yields the synthetic, unpersisted principal `dev`, which is still subject to
deny-by-default policy. A loud structured warning is logged once.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mcprouter.auth.keys import hash_key
from mcprouter.models import AgentPrincipal
from mcprouter.settings import Settings

log = logging.getLogger(__name__)

ADMIN_TOKEN_ENV = "MCPR_ADMIN_TOKEN"
DEV_AGENT_ID = "dev"

_dev_warned = False
_dev_lock = threading.Lock()


@dataclass(frozen=True)
class SecurityConfig:
    agent_keys_configured: bool
    admin_token_hash: str | None
    max_exposed_tools: int

    @classmethod
    def build(cls, settings: Settings, env: Mapping[str, str]) -> SecurityConfig:
        """Pure: everything comes from the arguments (no os.environ reads here).

        The admin token comes from `settings.admin_token` (MCPR_ADMIN_TOKEN, P-607);
        `env` is the fallback for callers that build Settings without it."""
        admin = settings.admin_token.strip() or env.get(ADMIN_TOKEN_ENV, "").strip()
        return cls(
            agent_keys_configured=bool(settings.agent_keys.strip()),
            admin_token_hash=hash_key(admin) if admin else None,
            max_exposed_tools=settings.max_exposed_tools,
        )

    @property
    def dev_mode_possible(self) -> bool:
        return not self.agent_keys_configured and self.admin_token_hash is None


def _warn_dev_once() -> None:
    global _dev_warned
    with _dev_lock:
        if _dev_warned:
            return
        _dev_warned = True
    log.warning(
        "auth.dev_mode event=auth_disabled reason=no_agent_keys_no_admin_token_no_principals "
        "identity=%s — every unauthenticated caller is the '%s' agent; dev only, never expose "
        "beyond localhost",
        DEV_AGENT_ID,
        DEV_AGENT_ID,
    )


def _reset_dev_warning_for_tests() -> None:
    global _dev_warned
    with _dev_lock:
        _dev_warned = False


def dev_mode_active(session: Session, config: SecurityConfig) -> bool:
    """True only when nothing is configured AND no principal exists."""
    if not config.dev_mode_possible:
        return False
    count = session.scalar(select(func.count()).select_from(AgentPrincipal)) or 0
    return count == 0


def dev_principal(config: SecurityConfig) -> AgentPrincipal:
    return AgentPrincipal(
        id="dev",
        agent_id=DEV_AGENT_ID,
        key_hash="",
        enabled=True,
        max_tools=config.max_exposed_tools,
    )


# Old private spelling (policy/scope.py, routes_route.py imported it).
_dev_mode_active = dev_mode_active
