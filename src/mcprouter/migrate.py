"""Schema migrations: the ONE schema-init path (D5).

`init_db` (db.py) calls `upgrade_to_head(engine)`:

* steady state (database already at head): two catalog reads, ZERO DDL, no lock;
* otherwise, under a session-level `pg_advisory_lock` (so concurrent processes
  migrate one at a time and the losers find the work done):
  - empty database                    -> `upgrade head` (0001 baseline + later);
  - pre-0.6 database (our tables, no `alembic_version`)
                                      -> legacy bridge once, `stamp 0001`,
                                         then `upgrade head`;
  - revision unknown to this build    -> `SchemaVersionError` (curated, FATAL):
                                         the database is newer than the code.

DDL runs with `lock_timeout` so a migration queued behind a long transaction
fails loudly instead of stalling every query behind its lock request.

CLI: `python -m mcprouter.migrate [upgrade|downgrade REV|current|heads]`
(reads MCPR_DATABASE_URL like the app).
"""

from __future__ import annotations

import argparse
import functools
import logging
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

SCRIPT_LOCATION = str(Path(__file__).resolve().parent / "migrations")
BASELINE_REVISION = "0001"
MIGRATION_LOCK_KEY = 0x6D637072_6D696772  # "mcpr" "migr"; session-level advisory lock
# Per DDL statement waiting for a table lock (literal SQL, no composition).
SET_LOCK_TIMEOUT_SQL = "SET lock_timeout = '30s'"
LOCK_WAIT_S = 600.0  # how long a second process waits for the first one's migration
_POLL_S = 0.2

log = logging.getLogger(__name__)

# Indexes created by raw SQL in migrations, not declared on the ORM models
# (expression GIN, HNSW, DESC composite). Excluded from metadata comparison;
# tests assert each one exists after `upgrade head`.
MIGRATION_ONLY_INDEXES: frozenset[str] = frozenset(
    {
        "ix_tools_fts",
        "ux_dup_pair",
        "ix_tools_routing_fts",
        "ix_skills_routing_fts",
        "ix_mcp_tools_embedding_hnsw",
        "ix_skills_embedding_hnsw",
        "ix_routing_decisions_agent_latest",
        "ix_agent_principals_key_hash",
    }
)

SchemaState = Literal["empty", "legacy", "versioned"]


class SchemaVersionError(RuntimeError):
    """The database is at a revision this build does not know. Curated message:
    names the revision only (never the DSN)."""

    def __init__(self, revision: str) -> None:
        super().__init__(
            f"database schema is newer than this build (revision {revision!r}); "
            "run a newer image or restore the pre-upgrade backup"
        )
        self.revision = revision


class BaselineDowngradeRefused(RuntimeError):
    """A downgrade below the 0001 baseline would drop every table."""

    def __init__(self) -> None:
        super().__init__(
            f"refusing to downgrade below the baseline {BASELINE_REVISION}; restore a backup"
        )


class MigrationLockTimeout(RuntimeError):
    """Another process held the migration lock for longer than LOCK_WAIT_S."""


def include_object(
    obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any
) -> bool:
    """Alembic hook: ignore the raw-SQL indexes the models do not declare."""
    del obj, reflected, compare_to
    return not (type_ == "index" and name in MIGRATION_ONLY_INDEXES)


def alembic_config(connection: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", SCRIPT_LOCATION)
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


@functools.cache
def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert head is not None  # the package always ships at least 0001
    return head


@functools.cache
def known_revisions() -> frozenset[str]:
    script = ScriptDirectory.from_config(alembic_config())
    return frozenset(r.revision for r in script.walk_revisions())


def current_revision(conn: Connection) -> str | None:
    return MigrationContext.configure(conn).get_current_revision()


def schema_state(conn: Connection) -> SchemaState:
    if conn.execute(text("SELECT to_regclass('alembic_version')")).scalar() is not None:
        if current_revision(conn) is not None:
            return "versioned"
    # ANY table this app owns (not just the core ones): a database where only a
    # side table exists must take the idempotent bridge, not a CREATE TABLE
    # that fails on every boot.
    from mcprouter.models import ALL_METADATA

    for table in sorted({t for md in ALL_METADATA for t in md.tables}):
        if conn.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar() is not None:
            return "legacy"
    return "empty"


def _check_known(revision: str | None) -> None:
    if revision is not None and revision not in known_revisions():
        raise SchemaVersionError(revision)


@contextmanager
def migration_lock(engine: Engine, wait_s: float = LOCK_WAIT_S) -> Iterator[None]:
    """Session-level advisory lock on a dedicated AUTOCOMMIT connection.

    Polled with pg_try_advisory_lock rather than a blocking pg_advisory_lock:
    a backend blocked inside a statement holds a snapshot, and
    CREATE INDEX CONCURRENTLY in the lock holder waits for every older
    snapshot — a blocking waiter would deadlock against it."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        deadline = time.monotonic() + wait_s
        while not conn.execute(
            text("SELECT pg_try_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY}
        ).scalar():
            if time.monotonic() >= deadline:
                raise MigrationLockTimeout(
                    "timed out waiting for another process's schema migration"
                )
            time.sleep(_POLL_S)
        try:
            yield
        finally:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
            except Exception as exc:  # noqa: BLE001 — never mask the migration's own error
                # Session lock: it is released anyway when this connection closes.
                log.warning("migrate.unlock_failed: %s", type(exc).__name__)


@contextmanager
def _ddl_connection(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as conn:
        conn.execute(text(SET_LOCK_TIMEOUT_SQL))
        conn.commit()
        try:
            yield conn
        finally:
            try:
                conn.rollback()
                conn.execute(text("RESET lock_timeout"))
                conn.commit()
            except Exception as exc:  # noqa: BLE001 — never mask the migration's own error
                conn.invalidate()  # do not return a connection with a stale lock_timeout
                log.warning("migrate.reset_failed: %s", type(exc).__name__)


def _upgrade_locked(engine: Engine, target: str) -> str | None:
    """Runs with the migration lock held. Returns the revision now current."""
    with _ddl_connection(engine) as conn:
        state = schema_state(conn)
        conn.commit()
        if state == "versioned":
            _check_known(current_revision(conn))
            conn.commit()
        elif state == "legacy":
            from mcprouter.migrations.legacy_bridge import run_bridge

            run_bridge(conn)
            conn.commit()
            command.stamp(alembic_config(conn), BASELINE_REVISION)
            conn.commit()
        command.upgrade(alembic_config(conn), target)
        conn.commit()
        rev = current_revision(conn)
        conn.commit()
        return rev


def upgrade_to_head(engine: Engine) -> None:
    """The startup path. Zero DDL (and no lock) when already at head."""
    head = head_revision()
    with engine.connect() as conn:
        if schema_state(conn) == "versioned":
            rev = current_revision(conn)
            _check_known(rev)
            if rev == head:
                return
    with migration_lock(engine):
        _upgrade_locked(engine, "head")


def _refuse_below_baseline(current: str | None, target: str) -> None:
    """Refuse BEFORE alembic runs anything: with transaction_per_migration the
    steps above the baseline would otherwise commit before 0001 refuses."""
    if target == "base":
        raise BaselineDowngradeRefused()
    if target.startswith("-") and target[1:].isdecimal():
        script = ScriptDirectory.from_config(alembic_config())
        rev = current
        for _ in range(int(target[1:])):
            if rev is None or rev == BASELINE_REVISION:
                raise BaselineDowngradeRefused()
            down = script.get_revision(rev).down_revision
            rev = down if isinstance(down, str) or down is None else down[0]


def downgrade(engine: Engine, target: str) -> str | None:
    with migration_lock(engine), _ddl_connection(engine) as conn:
        current = current_revision(conn)
        _check_known(current)
        _refuse_below_baseline(current, target)
        conn.commit()
        command.downgrade(alembic_config(conn), target)
        conn.commit()
        rev = current_revision(conn)
        conn.commit()
        return rev


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mcprouter.migrate",
        description="Apply or inspect MCP Router schema migrations (MCPR_DATABASE_URL).",
    )
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("upgrade", help="upgrade to head (the same path the app runs at startup)")
    down = sub.add_parser("downgrade", help="downgrade to REV (e.g. -1, or 0001)")
    down.add_argument("rev")
    sub.add_parser("current", help="print the database revision")
    sub.add_parser("heads", help="print the newest revision this build ships")
    args = parser.parse_args(argv)
    cmd = args.cmd or "upgrade"
    if cmd == "heads":
        print(head_revision())
        return 0

    from mcprouter.db import make_engine
    from mcprouter.settings import Settings

    engine = make_engine(Settings.from_env())
    try:
        if cmd == "upgrade":
            upgrade_to_head(engine)
            with engine.connect() as conn:
                print(current_revision(conn))
        elif cmd == "downgrade":
            print(downgrade(engine, args.rev))
        else:
            with engine.connect() as conn:
                print(current_revision(conn) or "(none)")
    except (
        SchemaVersionError,
        MigrationLockTimeout,
        BaselineDowngradeRefused,
        CommandError,
    ) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    except DBAPIError as exc:
        # Driver/server failures (lock_timeout, a migration script error, a
        # dropped connection): the entrypoint promises a FATAL line and the
        # exception text can carry the DSN, so only the class name is shown.
        print(
            f"FATAL: database error during migration ({type(exc).__name__}); see docs/upgrade.md",
            file=sys.stderr,
        )
        return 1
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
