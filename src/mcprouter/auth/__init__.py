"""Auth core (FR-07, P-607): no FastAPI, no `mcprouter.api` imports.

* `keys`       — key format, hashing, MCPR_AGENT_KEYS parsing, Bearer parsing;
* `config`     — SecurityConfig, dev-mode detection + its one-time warning;
* `principals` — env bootstrap, credential -> principal, admin check.

`mcprouter.api.deps_auth` keeps only the FastAPI dependencies and re-exports
every name it used to define (a façade), so old imports keep resolving.
Lower layers (execution, policy, gateway, registry, db) import from here.
"""

from __future__ import annotations

from mcprouter.auth.config import (
    ADMIN_TOKEN_ENV,
    DEV_AGENT_ID,
    SecurityConfig,
    dev_mode_active,
    dev_principal,
)
from mcprouter.auth.keys import (
    AGENT_ID_PATTERN,
    MAX_KEY_LEN,
    AgentKeysConfigError,
    AuthenticationError,
    generate_key,
    hash_key,
    parse_agent_keys,
    parse_bearer,
)
from mcprouter.auth.principals import (
    bootstrap_principals,
    check_admin,
    is_admin_bearer,
    resolve_principal,
)

__all__ = [
    "ADMIN_TOKEN_ENV",
    "AGENT_ID_PATTERN",
    "DEV_AGENT_ID",
    "MAX_KEY_LEN",
    "AgentKeysConfigError",
    "AuthenticationError",
    "SecurityConfig",
    "bootstrap_principals",
    "check_admin",
    "dev_mode_active",
    "dev_principal",
    "generate_key",
    "hash_key",
    "is_admin_bearer",
    "parse_agent_keys",
    "parse_bearer",
    "resolve_principal",
]
