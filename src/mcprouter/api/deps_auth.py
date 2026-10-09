"""Agent and admin authentication for REST and the MCP gateway (FR-07).

Agents: `Authorization: Bearer <api key>`. Keys are stored as sha256 hex in
`AgentPrincipal.key_hash`; the presented key is hashed, looked up with ONE
indexed query on `key_hash`, and the hit is confirmed with
`hmac.compare_digest`. The lookup key is a hash of the secret, so its timing
reveals nothing usable. (The core lives in `mcprouter.auth`; this module keeps
the FastAPI dependencies and re-exports the rest.)

Rules (all mutation-checked in tests/test_gateway_auth.py):

* A presented-but-invalid credential is ALWAYS 401 — never a silent downgrade
  to dev mode or to another identity.
* Only the `Authorization` header is consulted. Identity-ish headers
  (`X-Agent-Id`, `X-Forwarded-User`, ...) are ignored.
* Dev mode (no auth) applies only when nothing at all is configured: empty
  `settings.agent_keys`, no `MCPR_ADMIN_TOKEN`, and zero principals in the DB.
  It yields the synthetic, unpersisted principal `dev`, which is still subject
  to deny-by-default policy. A loud structured warning is logged once.
* Admin: `Authorization: Bearer <MCPR_ADMIN_TOKEN>` (`settings.admin_token`). Unset admin token outside
  dev mode => admin endpoints are 403 (fail closed). An agent key is never an
  admin credential, and bootstrap refuses an agent key equal to the admin token.

The raw key is never stored, logged, or echoed in an error.
"""

from __future__ import annotations

# ---- façade (P-607): the auth core moved to mcprouter/auth/; every name this
# module used to define or import is re-exported so old imports keep working.
import hashlib  # noqa: F401
import hmac  # noqa: F401
import logging
import re  # noqa: F401
import secrets  # noqa: F401
import threading  # noqa: F401
from collections.abc import Mapping
from dataclasses import dataclass  # noqa: F401
from typing import Any

from fastapi import HTTPException, Request
from sqlalchemy import func, select  # noqa: F401
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.auth import config as _config
from mcprouter.auth.config import (  # noqa: F401
    ADMIN_TOKEN_ENV,
    DEV_AGENT_ID,
    SecurityConfig,
    _dev_lock,
    _dev_mode_active,
    _reset_dev_warning_for_tests,
    _warn_dev_once,
    dev_principal,
)
from mcprouter.auth.keys import (  # noqa: F401
    _AGENT_ID_RE,
    _KEY_RE,
    MAX_KEY_LEN,
    AgentKeysConfigError,
    AuthenticationError,
    generate_key,
    hash_key,
    parse_agent_keys,
    parse_bearer,
)
from mcprouter.auth.principals import (  # noqa: F401
    _match_principal,
    bootstrap_principals,
    check_admin,
    is_admin_bearer,
    resolve_principal,
)
from mcprouter.models import AgentPrincipal  # noqa: F401
from mcprouter.settings import Settings

log = logging.getLogger(__name__)


# Patch targets: monkeypatch mcprouter.auth.* (config/keys/principals), not
# this module — setting a name here (e.g. _dev_warned, _match_principal)
# only shadows the re-export and no longer affects the auth core.
def __getattr__(name: str) -> Any:
    # `_dev_warned` is rebound inside auth.config; read it live, not a copy.
    if name == "_dev_warned":
        return _config._dev_warned
    raise AttributeError(name)


def configure_security(app: Any, env: Mapping[str, str]) -> SecurityConfig:
    """Integrator entrypoint, called once in create_app AFTER app.state.settings,
    .engine and .session_factory exist: creates the gateway-owned tables,
    bootstraps env principals (hash only) and installs app.state.security.
    Raises AgentKeysConfigError on bad config (fail loudly at startup)."""
    from mcprouter.execution.models import init_security_db  # local: keep api->execution lazy

    settings: Settings = app.state.settings
    config = SecurityConfig.build(settings, env)
    init_security_db(app.state.engine)
    with app.state.session_factory() as session:
        bootstrap_principals(session, settings, config)
        session.commit()
    app.state.security = config
    return config


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


def security_of(request: Request) -> tuple[SecurityConfig, sessionmaker[Session]]:
    """Public accessor for the installed SecurityConfig + session factory
    (503 when the app never ran configure_security)."""
    return _security(request)


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


# Explicit façade surface (mypy no-implicit-reexport): every pre-move name.
__all__ = [
    "ADMIN_TOKEN_ENV",
    "AgentKeysConfigError",
    "AgentPrincipal",
    "Any",
    "AuthenticationError",
    "DEV_AGENT_ID",
    "HTTPException",
    "MAX_KEY_LEN",
    "Mapping",
    "Request",
    "SecurityConfig",
    "Session",
    "Settings",
    "_AGENT_ID_RE",
    "_KEY_RE",
    "_dev_lock",
    "_dev_mode_active",
    "_match_principal",
    "_reset_dev_warning_for_tests",
    "_security",
    "_unauthorized",
    "_warn_dev_once",
    "bootstrap_principals",
    "check_admin",
    "configure_security",
    "dataclass",
    "dev_principal",
    "func",
    "generate_key",
    "get_principal",
    "hash_key",
    "hashlib",
    "hmac",
    "is_admin_bearer",
    "log",
    "logging",
    "parse_agent_keys",
    "parse_bearer",
    "re",
    "require_admin",
    "resolve_principal",
    "secrets",
    "security_of",
    "select",
    "sessionmaker",
    "threading",
]
