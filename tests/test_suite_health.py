"""MT-7 suite health: isolated, wiped test DB (P-501/P-502); no fixed ports (P-504)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, table, text
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.routes_setup import AppSetting
from mcprouter.models import MCPServerRecord, RouteFeedback
from tests.support import db as testdb

ROOT = Path(__file__).resolve().parents[1]

# ------------------------------------------------------------------ database


def test_guard_refuses_a_real_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCPR_ALLOW_ANY_DB", raising=False)
    for name in ("mcprouter", "prod", "contest", "mcpr_tests"):
        with pytest.raises(RuntimeError, match="refusing to wipe"):
            testdb.assert_disposable(f"postgresql+psycopg://u:p@h/{name}")
    for name in ("mcprouter_test_w3", "mcprouter_test_template", "x_test", "test"):
        testdb.assert_disposable(f"postgresql+psycopg://u:p@h/{name}")
    monkeypatch.setenv("MCPR_ALLOW_ANY_DB", "1")
    testdb.assert_disposable("postgresql+psycopg://u:p@h/mcprouter")


def test_tests_never_use_the_configured_database() -> None:
    base = testdb.BASE_URL.database
    assert testdb.WORKER_DB != base and testdb.TEMPLATE_DB != base
    assert testdb.WORKER_DB.startswith(f"{base}_test_w")
    testdb.assert_disposable(testdb.TEST_DB_URL)


def _tables_in_db(s: Session) -> set[str]:
    names = set(
        s.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        ).scalars()
    )
    names.discard("alembic_version")  # migration bookkeeping (E6), not app data
    return names


def test_wipe_list_covers_every_table_in_the_database(db: sessionmaker[Session]) -> None:
    with db() as s:
        assert _tables_in_db(s) == set(testdb.all_table_names())


def test_every_table_is_empty_after_wipe(db: sessionmaker[Session]) -> None:
    with db() as s:
        s.add(MCPServerRecord(name="health", transport="stdio", stdio_command=["true"]))
        s.add(AppSetting(key="health", value="1"))
        s.add(
            RouteFeedback(
                route_request_id="r",
                agent_id="a",
                target_kind="tool",
                target_id="t",
                helpful=True,
                source="agent",
            )
        )
        s.commit()
    engine = db.kw["bind"]
    assert isinstance(engine, Engine)
    testdb.wipe_all(engine)
    with db() as s:
        counts = {
            name: s.execute(select(func.count()).select_from(table(name))).scalar_one()
            for name in _tables_in_db(s)
        }
    assert set(counts.values()) == {0}, {k: v for k, v in counts.items() if v}


# ------------------------------------------------------------------ fixed ports (P-504)

_PORT_ASSIGN = re.compile(r"(?i)^\s*[\w, ]*port[\w, ]*=\s*[\d, ]*\b8\d{3}\b")
_PORT_URL = re.compile(r"(127\.0\.0\.1|localhost):8\d{3}\b")

#: (file, line substring) -> why the literal may stay. Everything else binds
#: through tests/support/ports.py or testbed's OS-assigned ports.
FIXED_PORT_ALLOWLIST: dict[tuple[str, str], str] = {
    ("tests/test_testbed.py", "127.0.0.1:8600/github"): "pure URL arithmetic, nothing binds",
    ("tests/test_testbed.py", "127.0.0.1:8601/jenkins"): "pure URL arithmetic, nothing binds",
    ("tests/test_backends_remote.py", "localhost:8765/v1"): "URL validation input, never dialled",
    ("tests/test_discovery_connector.py", "127.0.0.1:8600/x/mcp"): "URL parse input, never dialled",
    ("tests/test_discovery_import.py", "127.0.0.1:8600/{n}"): "registration refused first",
    # TODO(integrator): E3 migrates these (test_gateway_*/test_edge_* fence);
    # delete the four rows once that lands. Until then they run as e2e
    # modules pinned to one xdist worker each (xdist_group, --dist loadgroup).
    ("tests/test_gateway_mcp.py", "127.0.0.1:8641"): "E3 fence (docstring)",
    ("tests/test_gateway_mcp.py", "PORT = 8641"): "E3 fence",
    ("tests/test_gateway_skills_e2e.py", "PORT = 8803"): "E3 fence",
    ("tests/test_edge_roundtrip.py", "PORT_A, PORT_LOOP = 8761, 8762"): "E3 fence",
}


def _fixed_port_hits() -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    for path in sorted([*ROOT.glob("tests/**/*.py"), *ROOT.glob("testbed/**/*.py")]):
        rel = path.relative_to(ROOT).as_posix()
        if rel == "tests/test_suite_health.py":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if _PORT_ASSIGN.search(line) or _PORT_URL.search(line):
                hits.append((rel, line.strip()))
    return hits


def test_no_new_fixed_ports() -> None:
    unexplained = [
        (f, line)
        for f, line in _fixed_port_hits()
        if not any(f == af and sub in line for af, sub in FIXED_PORT_ALLOWLIST)
    ]
    assert unexplained == [], "use tests/support/ports.py (free_port/bound_socket/run_app)"


def test_fixed_port_allowlist_has_no_stale_rows() -> None:
    hits = _fixed_port_hits()
    stale = [k for k in FIXED_PORT_ALLOWLIST if not any(k[0] == f and k[1] in ln for f, ln in hits)]
    assert stale == []
