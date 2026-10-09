"""Shared fixtures and suite policy. Integration tests need the compose db:

    docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d db

Tests never touch the database named in ``MCPR_DATABASE_URL``: they run on a
per-worker clone of a session template (tests/support/db.py). Unit tests that
don't touch the DB must not require it.

Markers (``--strict-markers``; see pyproject.toml):
  db     — added automatically to every test whose fixtures reach ``db``, and by
           ``requires_db``. Skipped when the server is unreachable, or a FAILURE
           when ``MCPR_REQUIRE_DB=1`` (CI sets it: a silent skip hides a broken DB).
  e2e    — real servers/transports; by module name (below). Timeout 300 s.
  slow   — loads/downloads real models (also needs ``MCPR_RUN_SLOW=1``).
  scale  — fleet/catalog scale runs; nightly only.
"""

from __future__ import annotations

import os
import re

import pytest

# Before the first import of tests.support: keep pytest's assert rewriting there.
pytest.register_assert_rewrite("tests.support")

from mcprouter.settings import Settings  # noqa: E402
from tests.support import db as _testdb  # noqa: E402
from tests.support.db import TEST_DB_URL  # noqa: E402

__all__ = ["TEST_DB_URL", "requires_db"]

# Shared fixture modules (tests/support/). sec_db lives in support.execution;
# db = per-worker test database (E5); settings/route_hits are E1/E2's.
pytest_plugins = [
    "tests.support.execution",
    "tests.support.db",
    "tests.support.settings",
    "tests.support.route_hits",
]

#: Marks a test as needing the database (kept as a name: 55 modules import it).
requires_db = pytest.mark.db

E2E_TIMEOUT_S = 300
# `-n auto` cap: each worker holds a few Postgres connections (session engine +
# per-app pools) and the server default max_connections is 100; on a 100+ core
# box an uncapped `auto` exhausts it ("too many clients already").
MAX_AUTO_WORKERS = 8
# Module-name rules for markers that live with the suite, not in each file.
# e2e modules bind real ports; each runs on ONE xdist worker (xdist_group +
# `--dist loadgroup`) so a module-level port is never bound twice at once.
_E2E_MODULE = re.compile(
    r"(^test_e2e_|_e2e$|_real$|^test_testbed$|^test_edge_roundtrip$|^test_gateway_mcp$)"
)
_SCALE_MODULE = re.compile(r"_scale$")


def _db_available() -> bool:
    return _testdb.state.ok


def _is_xdist_controller(config: pytest.Config) -> bool:
    return not hasattr(config, "workerinput") and config.getoption("dist", "no") != "no"


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int:
    return min(os.cpu_count() or 1, MAX_AUTO_WORKERS)


def pytest_configure(config: pytest.Config) -> None:
    controller = _is_xdist_controller(config)
    worker = hasattr(config, "workerinput")
    _testdb.point_env_at_worker_db()
    _testdb.provision(controller=controller, build=not controller and not worker)


def pytest_unconfigure(config: pytest.Config) -> None:
    if _testdb.state.ok and not _is_xdist_controller(config):
        _testdb.drop_worker_db()


# tryfirst: xdist's loadgroup scheduler reads `xdist_group` in its own
# collection hook, so ours must have run by then.
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        mod = getattr(item, "module", None)
        module = mod.__name__.rsplit(".", 1)[-1] if mod else ""
        if {"db", "_test_engine"} & set(getattr(item, "fixturenames", ())):
            item.add_marker(pytest.mark.db)
        if _E2E_MODULE.search(module):
            item.add_marker(pytest.mark.e2e)
            item.add_marker(pytest.mark.xdist_group(module))
            if item.get_closest_marker("timeout") is None:
                item.add_marker(pytest.mark.timeout(E2E_TIMEOUT_S))
        if _SCALE_MODULE.search(module):
            item.add_marker(pytest.mark.scale)


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("db") is None or _testdb.state.ok:
        return
    if os.environ.get("MCPR_REQUIRE_DB") == "1":
        pytest.fail(f"MCPR_REQUIRE_DB=1 but {_testdb.state.reason}", pytrace=False)
    pytest.skip(_testdb.state.reason)


@pytest.fixture(autouse=True)
def _dispose_engines(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN202 - generator fixture
    """Every create_app() builds its own engine + pool; without disposal a
    full run exhausts Postgres max_connections. Dispose what each test made."""
    from sqlalchemy import create_engine as real

    import mcprouter.db as db_mod

    made = []

    def tracking(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        eng = real(*args, **kwargs)
        made.append(eng)
        return eng

    monkeypatch.setattr(db_mod, "create_engine", tracking)
    yield
    for eng in made:
        eng.dispose()


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(database_url=TEST_DB_URL)
