"""Credential -> principal, env bootstrap, admin check (P-607 move).

Rules (all mutation-checked in tests/test_gateway_auth.py):

* A presented-but-invalid credential is ALWAYS 401 — never a silent downgrade
  to dev mode or to another identity.
* Only the `Authorization` header is consulted.
* Admin: `Authorization: Bearer <admin token>`. Unset admin token outside dev
  mode => admin endpoints are 403 (fail closed). An agent key is never an
  admin credential, and bootstrap refuses an agent key equal to the admin token.
"""

from __future__ import annotations

import hmac
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from mcprouter.auth.config import (
    SecurityConfig,
    _warn_dev_once,
    dev_mode_active,
    dev_principal,
)
from mcprouter.auth.keys import (
    AgentKeysConfigError,
    AuthenticationError,
    hash_key,
    parse_agent_keys,
    parse_bearer,
)
from mcprouter.models import AgentPrincipal
from mcprouter.settings import Settings

log = logging.getLogger("mcprouter.api.deps_auth")  # unchanged logger name


def bootstrap_principals(session: Session, settings: Settings, config: SecurityConfig) -> int:
    """Upsert principals from settings.agent_keys (hash only). Returns the count.

    Env-configured keys are operator config and win over an API-created key
    for the same agent_id (rotation = change the env and restart).
    """
    pairs = parse_agent_keys(settings.agent_keys)
    if len({key for _, key in pairs}) != len(pairs):
        raise AgentKeysConfigError("MCPR_AGENT_KEYS reuses one key for several agents")
    for agent_id, key in pairs:
        key_hash = hash_key(key)
        if config.admin_token_hash and hmac.compare_digest(key_hash, config.admin_token_hash):
            raise AgentKeysConfigError(
                f"MCPR_AGENT_KEYS key for agent '{agent_id}' equals MCPR_ADMIN_TOKEN; use distinct"
                " credentials"
            )
        existing = session.scalars(
            select(AgentPrincipal).where(AgentPrincipal.agent_id == agent_id)
        ).one_or_none()
        if existing is None:
            session.add(AgentPrincipal(agent_id=agent_id, key_hash=key_hash))
        elif not hmac.compare_digest(existing.key_hash, key_hash):
            existing.key_hash = key_hash
    session.flush()
    if pairs:
        log.info("auth.bootstrap principals=%d", len(pairs))
    return len(pairs)


def _match_principal(session: Session, token: str) -> AgentPrincipal | None:
    presented = hash_key(token)
    # ONE indexed query (ix_agent_principals_key_hash, migration 0002) instead
    # of a full-table scan per request. The lookup key is the SHA-256 of the
    # presented secret, so index timing reveals nothing usable about any stored
    # key; the hit is still confirmed with compare_digest. LIMIT 2 is enough to
    # see a key shared by two principals.
    candidates = session.scalars(
        select(AgentPrincipal).where(AgentPrincipal.key_hash == presented).limit(2)
    ).all()
    matches = [p for p in candidates if hmac.compare_digest(presented, p.key_hash)]
    # Exactly one match or nothing: a key shared by two principals names no
    # one (never "last wins").
    if len(matches) != 1 or not matches[0].enabled:
        return None
    return matches[0]


def resolve_principal(
    session: Session, config: SecurityConfig, authorization: str | None
) -> AgentPrincipal:
    """The single agent-auth decision, shared by REST and the MCP gateway."""
    token = parse_bearer(authorization)
    if token is None:
        if dev_mode_active(session, config):
            _warn_dev_once()
            return dev_principal(config)
        raise AuthenticationError()
    principal = _match_principal(session, token)
    if principal is None:
        raise AuthenticationError()
    session.expunge(principal)
    return principal


def check_admin(session: Session, config: SecurityConfig, authorization: str | None) -> None:
    """Raises AuthenticationError (401) or PermissionError (403)."""
    if config.admin_token_hash is None:
        if dev_mode_active(session, config):
            _warn_dev_once()
            return
        raise PermissionError("admin token not configured")
    token = parse_bearer(authorization)
    if token is None or not hmac.compare_digest(hash_key(token), config.admin_token_hash):
        raise AuthenticationError("invalid or missing admin token")


def is_admin_bearer(config: SecurityConfig, authorization: str | None) -> bool:
    """True iff `authorization` is a well-formed Bearer whose token IS the admin
    token (constant-time compare). Never raises and never grants anything by
    itself: callers that branch on it still authenticate the other branch
    with resolve_principal. False when no admin token is configured."""
    if config.admin_token_hash is None:
        return False
    try:
        token = parse_bearer(authorization)
    except AuthenticationError:
        return False
    return token is not None and hmac.compare_digest(hash_key(token), config.admin_token_hash)
