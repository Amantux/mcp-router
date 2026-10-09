"""Background-loop ownership (P-602).

The SyncLoop and RollupLoop must run in exactly ONE process per database:
two replicas (or `uvicorn --workers 2`) would otherwise duplicate every sync
and rollup. `claim_loop_owner` takes a SESSION-level `pg_try_advisory_lock`
on a dedicated connection that stays checked out for as long as the engine
lives, so ownership ends exactly when the owning process (or engine) goes
away — Postgres releases the lock with the session. A process that loses
the race skips the loops and logs an ERROR (running several workers is a
misconfiguration; requests are still served).

Released by `release_loop_owner(engine)` (the app lifespan should call it on
shutdown), `engine.dispose()`, or garbage collection of the engine.
"""

from __future__ import annotations

import logging
import threading
import weakref
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

LOOP_OWNER_LOCK_KEY = 0x6D637072_6C6F6F70  # "mcpr" "loop"; session-level advisory lock

# One dedicated DBAPI connection per owning engine, OUTSIDE the engine's pool:
# a pooled connection would go back to the pool still holding the session
# lock if the engine were dropped without dispose(). weakref.finalize closes
# it (releasing the lock) when the engine is garbage-collected.
_held: weakref.WeakKeyDictionary[Engine, Any] = weakref.WeakKeyDictionary()
_mutex = threading.Lock()


def _close(dbapi_conn: Any) -> None:
    try:
        dbapi_conn.close()
    except Exception:  # noqa: BLE001 — best effort; closing the session frees the lock
        log.warning("singleton.release_failed")


def claim_loop_owner(engine: Engine) -> bool:
    """True when this process holds (or just took) the loop-owner lock."""
    with _mutex:
        if engine in _held:
            return True
        cargs, cparams = engine.dialect.create_connect_args(engine.url)
        dbapi_conn = engine.dialect.connect(*cargs, **cparams)
        try:
            dbapi_conn.autocommit = True
            cur = dbapi_conn.cursor()
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOOP_OWNER_LOCK_KEY,))
            row = cur.fetchone()
            cur.close()
        except Exception:
            _close(dbapi_conn)
            raise
        if not (row and row[0]):
            _close(dbapi_conn)
            log.error(
                "singleton.not_loop_owner: another process already runs the background "
                "sync/rollup loops for this database; this one skips them. Run ONE "
                "uvicorn worker per database."
            )
            return False
        _held[engine] = weakref.finalize(engine, _close, dbapi_conn)
        if not event.contains(engine, "engine_disposed", _on_dispose):
            event.listen(engine, "engine_disposed", _on_dispose)
        return True


def release_loop_owner(engine: Engine) -> None:
    """Give up ownership (idempotent). Closing the session releases the lock."""
    with _mutex:
        fin = _held.pop(engine, None)
    if fin is not None:
        fin()


def _on_dispose(engine: Engine) -> None:
    release_loop_owner(engine)
