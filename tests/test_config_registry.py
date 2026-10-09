"""MT-5 (config part): the settings registry, `.env.example`, compose and the
code agree. No database, no network."""

from __future__ import annotations

import ast
import re
from dataclasses import fields
from pathlib import Path

import pytest

from mcprouter import settings as settings_mod
from mcprouter.inference.engine import DECISION_BACKENDS, EMBEDDING_BACKENDS, MODE_CONCURRENCY
from mcprouter.inference.laya import NOUL_MODES
from mcprouter.settings import ENV_ONLY_SPEC, SETTINGS_SPEC, Settings
from tests.support.compose import COMPOSE_FILES, NON_ROUTER_COMPOSE, registry_vars

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / ".env.example"

# Commented `# VAR=default   # a | b | c` lines: the listed values must be the
# real accepted set (A4-009: the template once said `local`, a boot crash).
REAL_CHOICES: dict[str, set[str]] = {
    "MCPR_EMBEDDING_BACKEND": set(EMBEDDING_BACKENDS),
    "MCPR_DECISION_BACKEND": set(DECISION_BACKENDS),
    "MCPR_OPERATING_MODE": set(MODE_CONCURRENCY),
    "MCPR_LAYA_NOUL_MODE": set(NOUL_MODES),
    "MCPR_DEVICE": {"auto", "cpu", "cuda", "cuda:<index>"},
    "MCPR_LOG_FORMAT": {"console", "json"},
    "MCPR_LOG_LEVEL": {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
}
_LINE = re.compile(r"^#?\s*(MCPR_[A-Z0-9_]+|POSTGRES_[A-Z]+)=(\S*)\s*(?:#\s*(.*))?$")


def _example_entries() -> dict[str, tuple[str, str]]:
    """VAR -> (default value, trailing comment) for every VAR= line."""
    out: dict[str, tuple[str, str]] = {}
    for ln in EXAMPLE.read_text().splitlines():
        m = _LINE.match(ln.strip())
        if m:
            assert m.group(1) not in out, f"{m.group(1)} listed twice in .env.example"
            out[m.group(1)] = (m.group(2), m.group(3) or "")
    return out


def test_every_registry_var_in_env_example() -> None:
    entries = _example_entries()
    missing = sorted(v for v in registry_vars() if v not in entries)
    assert not missing, f"missing from .env.example: {missing}"


def test_env_example_lists_only_known_names() -> None:
    known = registry_vars() | NON_ROUTER_COMPOSE
    unknown = sorted(v for v in _example_entries() if v not in known)
    assert not unknown, f".env.example names unknown variables: {unknown}"


def test_every_compose_interpolation_in_env_example() -> None:
    entries = _example_entries()
    for name in COMPOSE_FILES:
        text = "\n".join(
            ln for ln in (ROOT / name).read_text().splitlines() if not ln.lstrip().startswith("#")
        )
        for var in re.findall(r"\$\{([A-Z0-9_]+)", text):
            assert var in entries, f"{name}: ${{{var}}} not in .env.example"


@pytest.mark.parametrize("var", sorted(REAL_CHOICES))
def test_env_example_enum_values_are_real(var: str) -> None:
    default, comment = _example_entries()[var]
    listed = {v.strip().split()[0] for v in comment.split("(")[0].split("|") if v.strip()}
    assert listed == REAL_CHOICES[var], f"{var}: template lists {listed}"
    assert default in REAL_CHOICES[var], f"{var}: template default {default!r}"


def test_required_block_first_and_empty() -> None:
    active = [
        ln.split("=", 1)
        for ln in EXAMPLE.read_text().splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    # Only the REQUIRED block is active, with empty values: CI appends real
    # values after `cp .env.example .env` (last assignment wins in dotenv).
    assert [k for k, _ in active] == ["MCPR_ADMIN_TOKEN", "MCPR_AGENT_KEYS", "POSTGRES_PASSWORD"]
    assert all(v == "" for _, v in active)
    assert "openssl rand -hex 32" in EXAMPLE.read_text()


# --- P-105: registry-driven parse + validation ---------------------------------

_NUMERIC_BAD = ["nan", "inf", "-inf", "-1", "abc", "1e999"]

# Every validated variable -> (a good value, bad values). The key set must
# equal the registry's typed set, so a new variable cannot skip validation.
VALIDATION_TABLE: dict[str, tuple[str, list[str]]] = {
    "MCPR_IDLE_UNLOAD_S": ("60", [*_NUMERIC_BAD, "0"]),
    "MCPR_EMBED_BATCH_SIZE": ("8", [*_NUMERIC_BAD, "0", "1.5"]),
    "MCPR_DECISION_TIMEOUT_S": ("0.5", [*_NUMERIC_BAD, "0"]),
    "MCPR_MAX_EXPOSED_TOOLS": ("4", [*_NUMERIC_BAD, "0"]),
    "MCPR_MAX_EXPOSED_SERVERS": ("2", [*_NUMERIC_BAD, "0"]),
    "MCPR_RETRIEVAL_CANDIDATES": ("10", [*_NUMERIC_BAD, "0"]),
    "MCPR_ROUTE_CONFIDENCE_FLOOR": ("0.5", [*_NUMERIC_BAD, "1.5"]),
    "MCPR_ROUTE_CACHE_TTL_S": ("0", _NUMERIC_BAD),
    "MCPR_ROUTE_CACHE_SIZE": ("0", _NUMERIC_BAD),
    "MCPR_PREFILL_MS_PER_1K_TOKENS": ("0", _NUMERIC_BAD),
    "MCPR_PRICE_PER_1K_INPUT_TOKENS": ("0.01", _NUMERIC_BAD),
    "MCPR_DEFAULT_TOOL_TIMEOUT_S": ("5", [*_NUMERIC_BAD, "0"]),
    "MCPR_RATE_LIMIT_PER_AGENT_PER_MIN": ("60", [*_NUMERIC_BAD, "0"]),
    "MCPR_MCP_MAX_SESSIONS": ("10", [*_NUMERIC_BAD, "0"]),
    "MCPR_MCP_MAX_SESSIONS_PER_AGENT": ("2", [*_NUMERIC_BAD, "0"]),
    "MCPR_MAX_EXPOSED_SKILLS": ("0", _NUMERIC_BAD),
    "MCPR_SKILL_BODY_MAX_BYTES": ("1024", [*_NUMERIC_BAD, "0"]),
    "MCPR_SKILL_RESOURCE_MAX_BYTES": ("1024", [*_NUMERIC_BAD, "0"]),
    "MCPR_DECISION_MAX_RETRIES": ("0", [*_NUMERIC_BAD, "11"]),
    "MCPR_DECISION_RATE_LIMIT_PER_MIN": ("30", [*_NUMERIC_BAD, "0"]),
    "MCPR_AOAI_MAX_RETRIES": ("3", [*_NUMERIC_BAD, "11"]),
    "MCPR_DEDUP_MAX_PAIRS": ("100", [*_NUMERIC_BAD, "0"]),
    "MCPR_EMBEDDING_BACKEND": ("BGE", ["local", "hash2"]),
    "MCPR_DECISION_BACKEND": ("remote", ["openai", "Laya2"]),
    "MCPR_OPERATING_MODE": ("battery", ["eco"]),
    "MCPR_LAYA_NOUL_MODE": ("native", ["both"]),
    "MCPR_LOG_LEVEL": ("debug", ["TRACE", "verbose"]),
    "MCPR_LOG_FORMAT": ("json", ["text"]),
    "MCPR_DEVICE": ("cuda:1", ["gpu", "cuda:x", "mps"]),
    "MCPR_SYNC_ENABLED": ("on", ["maybe"]),
    "MCPR_USAGE_PRIOR_ENABLED": ("1", ["2"]),
    "MCPR_ANALYTICS_ROLLUP_ENABLED": ("no", ["nope"]),
    "MCPR_ALLOW_OPEN_DEV": ("true", ["yes please"]),
    "MCPR_ALLOWED_HOSTS": ("router.lan,a.b:8443", ["evil host", "a/b", "*"]),
}
_TYPED = {"int", "opt_int", "float", "enum", "bool", "device", "hosts"}


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for k in list(os.environ):
        if k.startswith("MCPR_"):
            monkeypatch.delenv(k)


def test_validation_table_covers_every_typed_setting() -> None:
    typed = {s.env for s in SETTINGS_SPEC if s.type in _TYPED}
    assert set(VALIDATION_TABLE) == typed


@pytest.mark.parametrize(
    ("var", "bad"), [(v, b) for v, (_, bads) in VALIDATION_TABLE.items() for b in bads]
)
def test_bad_value_rejected_naming_the_var(
    monkeypatch: pytest.MonkeyPatch, var: str, bad: str
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(var, bad)
    with pytest.raises(ValueError) as ei:
        Settings.from_env()
    assert str(ei.value).startswith(f"{var}:"), str(ei.value)


@pytest.mark.parametrize("var", sorted(VALIDATION_TABLE))
def test_good_value_accepted(monkeypatch: pytest.MonkeyPatch, var: str) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv(var, VALIDATION_TABLE[var][0])
    Settings.from_env()


def test_registry_covers_every_field_once() -> None:
    names = [s.name for s in SETTINGS_SPEC]
    assert sorted(names) == sorted(f.name for f in fields(Settings))
    envs = [s.env for s in (*SETTINGS_SPEC, *ENV_ONLY_SPEC)]
    assert len(envs) == len(set(envs))
    for s in (*SETTINGS_SPEC, *ENV_ONLY_SPEC):
        assert s.doc.strip() and s.env.startswith("MCPR_")
        assert s.scope in ("app", "entrypoint", "compose")
        if s.type in ("enum", "device", "bool"):
            assert s.choices, s.env


def test_unset_env_equals_dataclass_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    assert Settings.from_env() == Settings()
    assert Settings.from_env({}) == Settings()


def test_registry_choices_match_engine_constants() -> None:
    assert settings_mod.EMBEDDING_BACKEND_CHOICES == EMBEDDING_BACKENDS
    assert settings_mod.DECISION_BACKEND_CHOICES == DECISION_BACKENDS
    assert set(settings_mod.OPERATING_MODE_CHOICES) == set(MODE_CONCURRENCY)
    assert settings_mod.LAYA_NOUL_MODE_CHOICES == NOUL_MODES


def test_enum_values_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("MCPR_EMBEDDING_BACKEND", " BGE ")
    monkeypatch.setenv("MCPR_LOG_LEVEL", "debug")
    s = Settings.from_env()
    assert (s.embedding_backend, s.log_level) == ("bge", "DEBUG")


# --- P-111 (MT-5): no config drift ---------------------------------------------

# MCPR_* names in code that are deliberately NOT settings (each with a reason).
NON_ROUTER = {
    "MCPR_AGENT_KEY": "scripts/smoke.sh input: the key part of one MCPR_AGENT_KEYS entry",
    "MCPR_AOAI": "prefix in messages/f-strings (MCPR_AOAI_*), not a variable",
    # Test-suite knobs (tests/conftest.py, tests/support/db.py, CI): never read
    # by the router, documented under "tooling" by scripts/gen_config_docs.py.
    "MCPR_RUN_SLOW": "pytest: run slow/live-model tests",
    "MCPR_REQUIRE_DB": "pytest: fail instead of skip when the test DB is down",
    "MCPR_ALLOW_ANY_DB": "pytest: allow wiping a non-*_test database",
    "MCPR_TEST_BASE_DATABASE_URL": "pytest-xdist: operator URL carried to workers",
    "MCPR_ENFORCE_ROUTE_COVERAGE": "pytest: MT-1 route coverage on full runs",
}
# Every os.environ / os.getenv use under src/, as file::function. A new env
# read must be added here deliberately (and almost always belongs in settings).
ENVIRON_ALLOWLIST = {
    "settings.py::from_env",  # Settings.from_env and AoaiSettings.from_env
    "settings.py::_entrypoint_main",
    "api/app.py::create_app",  # the env mapping deps_auth reads the token from
    "skills/gitsource.py::_default_runner",  # PATH only, for the git child
}
SRC = ROOT / "src" / "mcprouter"
_NAME_RE = re.compile(r"MCPR_[A-Z0-9_]*[A-Z0-9]")


def _code_names() -> dict[str, str]:
    found: dict[str, str] = {}
    paths = [
        *SRC.rglob("*.py"),
        *(ROOT / "scripts").glob("*"),
        *ROOT.glob("Dockerfile*"),
        *ROOT.glob("docker-compose*.yml"),
    ]
    for p in paths:
        if p.is_file() and "__pycache__" not in p.parts:
            for name in _NAME_RE.findall(p.read_text(errors="replace")):
                found.setdefault(name, str(p.relative_to(ROOT)))
    return found


def test_every_mcpr_name_in_code_is_registered() -> None:
    declared = registry_vars()
    stray = {n: f for n, f in _code_names().items() if n not in declared and n not in NON_ROUTER}
    assert not stray, f"MCPR_* used in code but not in SETTINGS_SPEC/ENV_ONLY_SPEC: {stray}"


def _environ_sites(node: ast.AST, rel: str, fn: str, out: set[str]) -> None:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        fn = node.name
    if (
        isinstance(node, ast.Attribute)
        and node.attr in ("environ", "getenv", "environb")
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    ):
        out.add(f"{rel}::{fn}")
    for child in ast.iter_child_nodes(node):
        _environ_sites(child, rel, fn, out)


def test_environ_reads_only_at_allowlisted_sites() -> None:
    sites: set[str] = set()
    for p in SRC.rglob("*.py"):
        _environ_sites(ast.parse(p.read_text()), str(p.relative_to(SRC)), "<module>", sites)
    assert sites == ENVIRON_ALLOWLIST
