"""Test databases: one template, one clone per xdist worker (D17, P-501/P-502).

``MCPR_DATABASE_URL`` names the *server* (and a base name, default
``mcprouter``); tests never touch that database. They use, on the same server:

* ``<base>_test_template`` — built once per pytest session (vector extension +
  ``init_db``) by the xdist controller, or by the process itself when serial;
* ``<base>_test_w<N>`` — ``CREATE DATABASE … TEMPLATE`` per worker (``gw<N>``;
  ``w0`` when serial), dropped at the end of the session.

Creating databases needs CREATEDB on the server (the compose/CI ``mcprouter``
role is a superuser). Admin statements are serialised with an advisory lock so
workers never clone while the template is being rebuilt. If the server is
unreachable, ``state.ok`` is False: ``db``-marked tests skip, or fail when
``MCPR_REQUIRE_DB=1`` (see tests/conftest.py).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import Engine, Table
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.ddl import sort_tables

from mcprouter.db import init_db, make_engine, make_session_factory
from mcprouter.settings import Settings

DEFAULT_URL = "postgresql+psycopg://mcprouter:mcprouter@localhost:5434/mcprouter"
#: Carries the operator's URL to xdist workers after ``point_env_at_worker_db``
#: has overwritten MCPR_DATABASE_URL in the controller's environment.
BASE_ENV = "MCPR_TEST_BASE_DATABASE_URL"
BASE_URL = make_url(os.environ.get(BASE_ENV) or os.environ.get("MCPR_DATABASE_URL", DEFAULT_URL))
_LOCK_KEY = 0x6D637072  # "mcpr": serialises template build vs. clones


def _worker_index() -> int:
    m = re.fullmatch(r"gw(\d+)", os.environ.get("PYTEST_XDIST_WORKER", ""))
    return int(m.group(1)) if m else 0


PREFIX = f"{BASE_URL.database}_test"
TEMPLATE_DB = f"{PREFIX}_template"
WORKER_DB = f"{PREFIX}_w{_worker_index()}"
TEST_DB_URL = BASE_URL.set(database=WORKER_DB).render_as_string(hide_password=False)


@dataclass
class DbState:
    ok: bool = False
    reason: str = "test database not provisioned"


state = DbState()


# ------------------------------------------------------------------ guard (P-501)

_TEST_NAME = re.compile(r"(^|_)test(_|$)")


def assert_disposable(url: str) -> None:
    """Refuse destructive cleanup on anything but a test database.

    A name counts as disposable when ``test`` is one of its ``_``-separated
    words (``mcprouter_test_w3``, ``x_test``). ``MCPR_ALLOW_ANY_DB=1`` overrides."""
    name = make_url(url).database or ""
    if _TEST_NAME.search(name) or os.environ.get("MCPR_ALLOW_ANY_DB") == "1":
        return
    raise RuntimeError(
        f"refusing to wipe database {name!r}: not a test database "
        "(name must contain a '_test' word, or set MCPR_ALLOW_ANY_DB=1)"
    )


def all_tables() -> list[Table]:
    """Every table the app owns (``models.ALL_METADATA``, the one list that
    migrations and init_db also use), children before parents (a safe order
    for row deletion)."""
    from mcprouter.models import ALL_METADATA

    tables = [t for md in ALL_METADATA for t in md.tables.values()]
    return list(reversed(sort_tables(tables)))


def all_table_names() -> list[str]:
    return [t.name for t in all_tables()]


def wipe_all(engine: Engine) -> None:
    """Delete every row of every app table, in one transaction.

    Row deletes, not ``TRUNCATE``: measured on the compose server, truncating
    the ~17 tables costs ~0.66 s per call (new relfilenodes + fsync) against
    ~7 ms for row deletes on test-sized tables; over ~1000 db tests that is
    minutes. No test depends on sequence values (ids are UUIDs)."""
    assert_disposable(engine.url.render_as_string(hide_password=False))
    with engine.connect() as conn:
        dbapi = conn.connection.driver_connection
        assert isinstance(dbapi, psycopg.Connection)
        with dbapi.cursor() as cur:
            for name in all_table_names():
                cur.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(name)))
        # Raw-driver statements: SQLAlchemy never began a transaction here, so
        # its conn.commit() would be a no-op and close() would roll back.
        dbapi.commit()


# ------------------------------------------------------------------ provisioning


def _admin() -> psycopg.Connection[tuple[object, ...]]:
    url = BASE_URL.set(drivername="postgresql", database="postgres")
    return psycopg.connect(
        url.render_as_string(hide_password=False), autocommit=True, connect_timeout=3
    )


def _recreate(
    conn: psycopg.Connection[tuple[object, ...]], name: str, template: str | None
) -> None:
    conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
    if template is None:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    else:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(template)
            )
        )


def build_template() -> None:
    """(Re)create the template from the current code's schema."""
    with _admin() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            _recreate(conn, TEMPLATE_DB, None)
            url = BASE_URL.set(database=TEMPLATE_DB).render_as_string(hide_password=False)
            eng = make_engine(Settings(database_url=url))
            try:
                init_db(eng)
            finally:
                eng.dispose()  # a connected template cannot be cloned
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))


def clone_worker_db() -> None:
    with _admin() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            _recreate(conn, WORKER_DB, TEMPLATE_DB)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))


def drop_worker_db() -> None:
    with _admin() as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(WORKER_DB))
        )


def point_env_at_worker_db() -> None:
    """Make ``MCPR_DATABASE_URL`` this process's test database, so code that
    reads the environment (``Settings.from_env()``) can never reach the real
    database or another worker's."""
    os.environ.setdefault(BASE_ENV, BASE_URL.render_as_string(hide_password=False))
    os.environ["MCPR_DATABASE_URL"] = TEST_DB_URL


def provision(*, controller: bool, build: bool) -> None:
    """Called from ``pytest_configure``. ``controller``: xdist controller (builds
    the template, runs no tests). ``build``: this process must build the
    template itself (serial run)."""
    try:
        if controller or build:
            build_template()
        if not controller:
            clone_worker_db()
    except (psycopg.Error, OSError) as exc:
        state.ok = False
        state.reason = f"test database unavailable ({type(exc).__name__})"
        return
    state.ok = True
    state.reason = ""


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="session")
def _test_engine() -> Iterator[Engine]:
    eng = make_engine(Settings(database_url=TEST_DB_URL))
    yield eng
    eng.dispose()


@pytest.fixture()
def db(_test_engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Session factory on this worker's database (schema from the template).
    Every app table is emptied afterwards."""
    yield make_session_factory(_test_engine)
    wipe_all(_test_engine)
