"""Per-tool usage stats, updated by the execution manager after each call.

`avg_latency_ms` is an exponential moving average with EMA_ALPHA = 0.2:
    avg <- latency                         (first observation)
    avg <- (1 - 0.2) * avg + 0.2 * latency (thereafter)
i.e. roughly the last ~10 calls dominate, so a server that got slower shows up
within a handful of calls without one outlier swinging it. Failed calls count
toward latency too (a slow failure is still a slow tool).

The update is a single atomic SQL UPDATE (no read-modify-write), so
concurrent executions cannot lose increments. It does NOT bump `updated_at`,
which tracks metadata changes, not traffic.
"""

from __future__ import annotations

import math
from typing import Any, cast

from sqlalchemy import CursorResult, case, update
from sqlalchemy.orm import Session

from mcprouter.models import MCPToolRecord
from mcprouter.registry.errors import InvalidArgument

EMA_ALPHA = 0.2


def record_execution(session: Session, tool_id: str, *, ok: bool, latency_ms: float) -> bool:
    """Returns False if the tool does not exist (e.g. removed mid-call)."""
    if not math.isfinite(latency_ms) or latency_ms < 0:
        raise InvalidArgument("latency_ms must be a finite, non-negative number.")
    T = MCPToolRecord
    stmt = (
        update(T)
        .where(T.id == tool_id)
        .values(
            call_count=T.call_count + 1,
            error_count=T.error_count + (0 if ok else 1),
            avg_latency_ms=case(
                (T.avg_latency_ms.is_(None), latency_ms),
                else_=(1 - EMA_ALPHA) * T.avg_latency_ms + EMA_ALPHA * latency_ms,
            ),
            updated_at=T.updated_at,  # traffic is not a metadata change
        )
        .execution_options(synchronize_session=False)
    )
    result = cast(CursorResult[Any], session.execute(stmt))
    return result.rowcount == 1
