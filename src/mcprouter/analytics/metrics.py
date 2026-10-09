"""Prometheus funnel totals, recomputed from the DATABASE off the event loop.

Cache-independent in the sense that matters: values are recomputed from
routing_decisions / execution_records / tool_stats_daily (rollups for old
days, raw for the rest — the same merge as the API), never accumulated in
process memory, so they are correct across restarts and agree with the
analytics endpoints. The collector keeps only a short-lived snapshot (see
FunnelCollector) so a scrape never runs SQL on the event loop.

All-time totals, exposed as counters:

    mcpr_analytics_tools_surfaced_total
    mcpr_analytics_tools_selected_total
    mcpr_analytics_tools_succeeded_total
    mcpr_analytics_skills_surfaced_total
    mcpr_analytics_skills_activated_total

The tools_* counters count tools AND skills (kind-blind funnel); the
skills_* counters are the skill-only subsets. Same single collector.

No per-tool labels (cardinality). Registration is idempotent: ONE collector
per process is registered in the default registry; `bind()` points it at the
most recently installed app's session factory (tests re-create apps). A DB
failure keeps the last-good snapshot and logs the exception type — never a
failed scrape. Refresh cost grows with UNROLLED history: nothing schedules
rollups yet (POST /api/v1/analytics/rollup is on-demand), so run it
periodically.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable

from prometheus_client import REGISTRY
from prometheus_client.core import CounterMetricFamily, Metric
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.analytics.funnel import merged_funnel, totals
from mcprouter.analytics.tokens import tool_token_map
from mcprouter.analytics.window import all_time
from mcprouter.models import utcnow

log = logging.getLogger(__name__)

_NAMES = {
    "surfaced": (
        "mcpr_analytics_tools_surfaced",
        "Tool and skill appearances in routing decisions (skills included).",
    ),
    "selected": (
        "mcpr_analytics_tools_selected",
        "Surfaced (decision, tool|skill) pairs the agent then called/activated (attributed).",
    ),
    "succeeded": (
        "mcpr_analytics_tools_succeeded",
        "Selected (decision, tool|skill) pairs with at least one ok call.",
    ),
    # Skill-only subsets of surfaced/selected (funnel ids "skill:<id>").
    "skills_surfaced": (
        "mcpr_analytics_skills_surfaced",
        "Skill appearances in routing decisions (subset of tools_surfaced).",
    ),
    "skills_activated": (
        "mcpr_analytics_skills_activated",
        "Surfaced (decision, skill) pairs then activated (subset of tools_selected).",
    ),
}


REFRESH_S = 30.0
STATEMENT_TIMEOUT = "10s"


class FunnelCollector:
    """`collect()` NEVER touches the database: prometheus_client's ASGI app
    runs collection on the event loop (it does not offload to a thread), so a
    sync DB query there would stall every gateway session. Instead it serves
    the last computed totals and, when they are older than REFRESH_S, starts
    ONE background refresh thread (single-flight, statement_timeout-bounded).
    Values are therefore at most ~REFRESH_S + query time stale; the first
    scrape after start/bind exposes no samples until the first refresh lands."""

    def __init__(self) -> None:
        self._factory: sessionmaker[Session] | None = None
        self._lock = threading.Lock()
        self._values: dict[str, int] | None = None
        self._computed_at = 0.0
        self._refreshing = False

    def bind(self, factory: sessionmaker[Session]) -> None:
        with self._lock:
            self._factory = factory
            self._values, self._computed_at = None, 0.0

    def describe(self) -> Iterable[Metric]:
        # Names only (no DB access at registration time).
        return [CounterMetricFamily(name, doc) for name, doc in _NAMES.values()]

    def refresh_now(self) -> None:
        """Synchronous refresh (background thread body; also used by tests)."""
        with self._lock:
            factory = self._factory
        try:
            if factory is None:
                return
            with factory() as s:
                s.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
                f = merged_funnel(s, all_time(utcnow()), tool_token_map(s))
                s.rollback()
            t = totals(f)
            sk = [c for tid, c in f.items() if tid.startswith("skill:")]
            values = {
                "surfaced": t.surfaced,
                "selected": t.selected,
                "succeeded": t.succeeded,
                "skills_surfaced": sum(c.surfaced for c in sk),
                "skills_activated": sum(c.selected for c in sk),
            }
            with self._lock:
                if self._factory is factory:  # not re-bound meanwhile
                    self._values, self._computed_at = values, time.monotonic()
        except Exception as exc:  # noqa: BLE001 — metrics must never fail; keep last-good
            log.warning("analytics.metrics_refresh_failed exc_type=%s", type(exc).__name__)
        finally:
            with self._lock:
                self._refreshing = False

    def collect(self) -> Iterable[Metric]:
        with self._lock:
            values = self._values
            stale = time.monotonic() - self._computed_at > REFRESH_S
            start = stale and self._factory is not None and not self._refreshing
            if start:
                self._refreshing = True
        if start:
            threading.Thread(
                target=self.refresh_now, name="mcpr-analytics-metrics", daemon=True
            ).start()
        if values is None:
            return []
        return [
            CounterMetricFamily(name, doc, value=values[key]) for key, (name, doc) in _NAMES.items()
        ]


_collector: FunnelCollector | None = None
_register_lock = threading.Lock()


def install_metrics(factory: sessionmaker[Session]) -> FunnelCollector:
    """Idempotent: registers once per process, rebinds on every call."""
    global _collector
    with _register_lock:
        if _collector is None:
            collector = FunnelCollector()
            REGISTRY.register(collector)
            _collector = collector
        _collector.bind(factory)
        return _collector
