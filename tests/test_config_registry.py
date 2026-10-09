"""MT-5 (config part): the settings registry, `.env.example`, compose and the
code agree. No database, no network."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from mcprouter.inference.engine import DECISION_BACKENDS, EMBEDDING_BACKENDS, MODE_CONCURRENCY
from mcprouter.inference.laya import NOUL_MODES
from tests.test_compose_contract import COMPOSE_FILES, NON_ROUTER_COMPOSE, registry_vars

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
