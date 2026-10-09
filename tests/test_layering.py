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
    # Permanent by design (P-206): a re-export façade over mcprouter.api so old
    # imports keep resolving (pinned by test_rest_gaps_acting); no src imports it.
    "registry/api_deps.py": "E2 (P-206): deprecated re-export façade of api/deps.py",
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


def _package_of(path: Path) -> list[str]:
    parts = ["mcprouter", *path.relative_to(SRC).with_suffix("").parts]
    return parts if path.name == "__init__.py" else parts[:-1]


def _absolute(node: ast.ImportFrom, path: Path) -> str:
    if node.level == 0:
        return node.module or ""
    base = _package_of(path)
    base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
    return ".".join([*base, *([node.module] if node.module else [])])


def _is_api(mod: str) -> bool:
    return mod == "mcprouter.api" or mod.startswith("mcprouter.api.")


def _api_imports(path: Path, source: str | None = None) -> list[str]:
    """Every `mcprouter.api...` import in the file: top-level or function-local,
    absolute or relative, and importlib/__import__ calls with a literal name."""
    found: list[str] = []
    text = path.read_text() if source is None else source
    for node in ast.walk(ast.parse(text, str(path))):
        if (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and _is_api(node.args[0].value)
            and (getattr(node.func, "attr", None) or getattr(node.func, "id", None))
            in {"import_module", "__import__"}
        ):
            found.append(f"{node.lineno}: dynamic import {node.args[0].value}")
        if isinstance(node, ast.ImportFrom):
            mod = _absolute(node, path)
            if mod == "mcprouter" and any(a.name == "api" for a in node.names):
                found.append(f"{node.lineno}: from mcprouter import api")
            elif _is_api(mod):
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


AUTH_MAY_IMPORT = {"mcprouter.auth", "mcprouter.models", "mcprouter.settings"}


def test_auth_core_imports_only_models_and_settings() -> None:
    """auth/ sits under execution/policy/gateway; importing them would cycle."""
    for path in sorted((SRC / "auth").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                mod = _absolute(node, path)
            elif isinstance(node, ast.Import):
                mod = node.names[0].name
            else:
                continue
            if mod.startswith("mcprouter"):
                assert any(mod == a or mod.startswith(a + ".") for a in AUTH_MAY_IMPORT), (
                    f"{path.name}: {mod}"
                )


def test_layering_scanner_sees_relative_and_dynamic_imports() -> None:
    probe = (
        "from ..api import deps_auth\n"
        "from ..api.deps_auth import require_admin\n"
        "import importlib\n"
        "importlib.import_module('mcprouter.api.deps_auth')\n"
        "from .ratelimit import SlidingWindowLimiter\n"
    )
    hits = _api_imports(SRC / "execution" / "probe.py", probe)
    assert len(hits) == 3, hits


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
