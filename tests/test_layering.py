"""MT-8 (layering half): nothing below the API layer imports `mcprouter.api`,
and the `api/deps_auth.py` façade keeps every pre-move name (P-607, D10)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "mcprouter"

# Pending switches owned by other fences; each entry is a debt with an owner.
# Delete the entry when the owner switches the import (the test then guards it).
PENDING: dict[str, str] = {
    "gateway/server.py": "E3: import auth core from mcprouter.auth (AuthenticationError, "
    "SecurityConfig, hash_key, resolve_principal)",
    "registry/api_deps.py": "E2 (P-206): FastAPI deps belong in api/deps.py; this module "
    "becomes a re-export façade there",
}

# The composition root is allowed to import the API layer by definition.
COMPOSITION = {"api"}

# Every name `mcprouter.api.deps_auth` defined or imported before the move.
FACADE_NAMES = frozenset(
    {
        "ADMIN_TOKEN_ENV", "AgentKeysConfigError", "AgentPrincipal", "Any",
        "AuthenticationError", "DEV_AGENT_ID", "HTTPException", "MAX_KEY_LEN", "Mapping",
        "Request", "SecurityConfig", "Session", "Settings", "_AGENT_ID_RE", "_KEY_RE",
        "_dev_lock", "_dev_mode_active", "_dev_warned", "_match_principal",
        "_reset_dev_warning_for_tests", "_security", "_unauthorized", "_warn_dev_once",
        "annotations", "bootstrap_principals", "check_admin", "configure_security",
        "dataclass", "dev_principal", "func", "generate_key", "get_principal", "hash_key",
        "hashlib", "hmac", "is_admin_bearer", "log", "logging", "parse_agent_keys",
        "parse_bearer", "re", "require_admin", "resolve_principal", "secrets",
        "security_of", "select", "sessionmaker", "threading",
    }
)  # fmt: skip


def _api_imports(path: Path) -> list[str]:
    """Every `mcprouter.api...` import in the file, top-level or function-local."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mod = node.module
            if mod == "mcprouter" and any(a.name == "api" for a in node.names):
                found.append(f"{node.lineno}: from mcprouter import api")
            elif mod == "mcprouter.api" or mod.startswith("mcprouter.api."):
                found.append(f"{node.lineno}: from {mod} import ...")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "mcprouter.api" or a.name.startswith("mcprouter.api."):
                    found.append(f"{node.lineno}: import {a.name}")
    return found


def _offenders() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.split("/", 1)[0] in COMPOSITION:
            continue
        hits = _api_imports(path)
        if hits:
            out[rel] = hits
    return out


def test_no_lower_layer_imports_the_api_layer() -> None:
    offenders = {k: v for k, v in _offenders().items() if k not in PENDING}
    assert offenders == {}, (
        "modules outside mcprouter/api import mcprouter.api (import from mcprouter.auth "
        f"or move the code up): {offenders}"
    )


def test_pending_allowlist_has_no_stale_entries() -> None:
    stale = sorted(set(PENDING) - set(_offenders()))
    assert stale == [], f"remove from PENDING (no longer imports mcprouter.api): {stale}"


def test_auth_core_has_no_fastapi_or_api_dependency() -> None:
    for path in sorted((SRC / "auth").rglob("*.py")):
        tree = ast.parse(path.read_text())
        mods = {
            n.module
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module is not None
        } | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not {m for m in mods if m.split(".")[0] in {"fastapi", "starlette"}}, path
        assert _api_imports(path) == [], path


def test_deps_auth_facade_keeps_every_name() -> None:
    import mcprouter.api.deps_auth as facade

    missing = sorted(n for n in FACADE_NAMES if not hasattr(facade, n))
    assert missing == []
    current = {n for n in vars(facade) if not n.startswith("__")} | {"_dev_warned"}
    # Same member count as before the move: one helper alias (_config) stands in
    # for _dev_warned, which is now served live by the module __getattr__.
    assert len(current - {"_config"}) == len(FACADE_NAMES)


def test_dev_warning_flag_is_live_through_the_facade() -> None:
    import mcprouter.api.deps_auth as facade
    from mcprouter.auth import config

    facade._reset_dev_warning_for_tests()
    assert facade._dev_warned is False
    config._warn_dev_once()
    assert facade._dev_warned is True
    facade._reset_dev_warning_for_tests()


@pytest.mark.parametrize(
    "name", ["SecurityConfig", "resolve_principal", "hash_key", "_dev_mode_active", "DEV_AGENT_ID"]
)
def test_facade_names_are_the_auth_core_objects(name: str) -> None:
    import mcprouter.api.deps_auth as facade
    import mcprouter.auth.config as c
    import mcprouter.auth.keys as k
    import mcprouter.auth.principals as p

    core = next(getattr(m, name) for m in (c, k, p) if hasattr(m, name))
    assert getattr(facade, name) is core
