"""Agent and admin authentication for REST and the MCP gateway (FR-07).

Agents: `Authorization: Bearer <api key>`. Keys are stored as sha256 hex in
`AgentPrincipal.key_hash`; the presented key is hashed and compared against
EVERY principal's hash with `hmac.compare_digest` (no early exit), so timing
reveals neither which principal matched nor how much of a hash matched.

Rules (all mutation-checked in tests/test_gateway_auth.py):

* A presented-but-invalid credential is ALWAYS 401 — never a silent downgrade
  to dev mode or to another identity.
* Only the `Authorization` header is consulted. Identity-ish headers
  (`X-Agent-Id`, `X-Forwarded-User`, ...) are ignored.
* Dev mode (no auth) applies only when nothing at all is configured: empty
  `settings.agent_keys`, no `MCPR_ADMIN_TOKEN`, and zero principals in the DB.
  It yields the synthetic, unpersisted principal `dev`, which is still subject
  to deny-by-default policy. A loud structured warning is logged once.
* Admin: `Authorization: Bearer <MCPR_ADMIN_TOKEN>`. Unset admin token outside
  dev mode => admin endpoints are 403 (fail closed). An agent key is never an
  admin credential, and bootstrap refuses an agent key equal to the admin token.

The raw key is never stored, logged, or echoed in an error.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.models import AgentPrincipal
from mcprouter.settings import Settings

log = logging.getLogger(__name__)

ADMIN_TOKEN_ENV = "MCPR_ADMIN_TOKEN"
DEV_AGENT_ID = "dev"
MAX_KEY_LEN = 512
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
_KEY_RE = re.compile(r"^[\x21-\x7e]{1,512}$")  # printable ASCII, no spaces

_dev_warned = False
_dev_lock = threading.Lock()


class AgentKeysConfigError(ValueError):
    """Malformed MCPR_AGENT_KEYS. The message never contains key material."""


class AuthenticationError(Exception):
    """No valid credential. Curated message; maps to 401."""

    def __init__(self, message: str = "invalid or missing API key") -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class SecurityConfig:
    agent_keys_configured: bool
    admin_token_hash: str | None
    max_exposed_tools: int

    @classmethod
    def build(cls, settings: Settings, env: Mapping[str, str]) -> SecurityConfig:
        """Pure: everything comes from the arguments (no os.environ reads here)."""
        admin = env.get(ADMIN_TOKEN_ENV, "").strip()
        return cls(
            agent_keys_configured=bool(settings.agent_keys.strip()),
            admin_token_hash=hash_key(admin) if admin else None,
            max_exposed_tools=settings.max_exposed_tools,
        )

    @property
    def dev_mode_possible(self) -> bool:
        return not self.agent_keys_configured and self.admin_token_hash is None


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return "mcpr_" + secrets.token_urlsafe(32)


def parse_agent_keys(spec: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for idx, raw in enumerate(p.strip() for p in spec.split(",")):
        if not raw:
            continue
        agent_id, sep, key = raw.partition(":")
        if not sep or not _AGENT_ID_RE.match(agent_id) or not _KEY_RE.match(key):
            raise AgentKeysConfigError(
                f"MCPR_AGENT_KEYS entry #{idx + 1} is malformed; expected 'agent_id:key' "
                "(agent_id [A-Za-z0-9_.-], key printable ASCII without spaces)"
            )
        if agent_id in seen:
            raise AgentKeysConfigError(f"MCPR_AGENT_KEYS lists agent '{agent_id}' twice")
        seen.add(agent_id)
        pairs.append((agent_id, key))
    return pairs


def bootstrap_principals(session: Session, settings: Settings, config: SecurityConfig) -> int:
    """Upsert principals from settings.agent_keys (hash only). Returns the count.

    Env-configured keys are operator config and win over an API-created key
    for the same agent_id (rotation = change the env and restart).
    """
    pairs = parse_agent_keys(settings.agent_keys)
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


def parse_bearer(header: str | None) -> str | None:
    """None if absent; raises AuthenticationError if present but malformed."""
    if header is None:
        return None
    scheme, sep, token = header.partition(" ")
    if not sep or scheme.lower() != "bearer" or not _KEY_RE.match(token):
        raise AuthenticationError()
    return token


def _match_principal(session: Session, token: str) -> AgentPrincipal | None:
    presented = hash_key(token)
    found: AgentPrincipal | None = None
    # Full scan with compare_digest and NO early exit (constant-time w.r.t.
    # which principal matched). Local-agent counts make this cheap.
    for p in session.scalars(select(AgentPrincipal)).all():
        if hmac.compare_digest(presented, p.key_hash):
            found = p
    if found is None or not found.enabled:
        return None
    return found


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


def _dev_mode_active(session: Session, config: SecurityConfig) -> bool:
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


def resolve_principal(
    session: Session, config: SecurityConfig, authorization: str | None
) -> AgentPrincipal:
    """The single agent-auth decision, shared by REST and the MCP gateway."""
    token = parse_bearer(authorization)
    if token is None:
        if _dev_mode_active(session, config):
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
        if _dev_mode_active(session, config):
            _warn_dev_once()
            return
        raise PermissionError("admin token not configured")
    token = parse_bearer(authorization)
    if token is None or not hmac.compare_digest(hash_key(token), config.admin_token_hash):
        raise AuthenticationError("invalid or missing admin token")


# ------------------------------------------------------------ FastAPI deps
def _security(request: Request) -> tuple[SecurityConfig, sessionmaker[Session]]:
    config = getattr(request.app.state, "security", None)
    factory = getattr(request.app.state, "session_factory", None)
    if not isinstance(config, SecurityConfig) or factory is None:
        # Fail closed: an app that forgot configure_security() serves no one.
        raise HTTPException(status_code=503, detail="security not configured")
    return config, factory


def _unauthorized(exc: AuthenticationError) -> HTTPException:
    return HTTPException(
        status_code=401, detail=exc.message, headers={"WWW-Authenticate": "Bearer"}
    )


def get_principal(request: Request) -> AgentPrincipal:
    config, factory = _security(request)
    with factory() as session:
        try:
            return resolve_principal(session, config, request.headers.get("authorization"))
        except AuthenticationError as exc:
            raise _unauthorized(exc) from None


def require_admin(request: Request) -> None:
    config, factory = _security(request)
    with factory() as session:
        try:
            check_admin(session, config, request.headers.get("authorization"))
        except AuthenticationError as exc:
            raise _unauthorized(exc) from None
        except PermissionError:
            raise HTTPException(status_code=403, detail="admin token not configured") from None
