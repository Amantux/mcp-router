"""Prometheus funnel totals, computed from the DATABASE at scrape time.

Cache-independent: values come from routing_decisions / execution_records /
tool_stats_daily (rollups for old days, raw for the rest — the same merge as
the API), so they are correct across restarts and identical to what the
analytics endpoints report. All-time totals, exposed as counters:

    mcpr_analytics_tools_surfaced_total
    mcpr_analytics_tools_selected_total
    mcpr_analytics_tools_succeeded_total

No per-tool labels (cardinality). Registration is idempotent: ONE collector
per process is registered in the default registry; `bind()` points it at the
most recently installed app's session factory (tests re-create apps). A DB
failure at scrape time yields no samples and a log line — never a failed
scrape. Scrape cost is bounded by rollup cadence (unrolled days are raw).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable

from prometheus_client import REGISTRY
from prometheus_client.core import CounterMetricFamily, Metric
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.analytics.funnel import merged_funnel, totals
from mcprouter.analytics.tokens import tool_token_map
from mcprouter.analytics.window import all_time
from mcprouter.models import utcnow

log = logging.getLogger(__name__)

_NAMES = {
    "surfaced": ("mcpr_analytics_tools_surfaced", "Tool appearances in routing decisions."),
    "selected": (
        "mcpr_analytics_tools_selected",
        "Surfaced (decision, tool) pairs the agent then called (attributed).",
    ),
    "succeeded": (
        "mcpr_analytics_tools_succeeded",
        "Selected (decision, tool) pairs with at least one ok call.",
    ),
}


class FunnelCollector:
    def __init__(self) -> None:
        self._factory: sessionmaker[Session] | None = None
        self._lock = threading.Lock()

    def bind(self, factory: sessionmaker[Session]) -> None:
        with self._lock:
            self._factory = factory

    def describe(self) -> Iterable[Metric]:
        # Names only (no DB access at registration time).
        return [CounterMetricFamily(name, doc) for name, doc in _NAMES.values()]

    def collect(self) -> Iterable[Metric]:
        with self._lock:
            factory = self._factory
        if factory is None:
            return []
        try:
            with factory() as s:
                now = utcnow()
                t = totals(merged_funnel(s, all_time(now), tool_token_map(s)))
        except Exception as exc:  # noqa: BLE001 — a scrape must never fail on the DB
            log.warning("analytics.metrics_collect_failed exc_type=%s", type(exc).__name__)
            return []
        values = {"surfaced": t.surfaced, "selected": t.selected, "succeeded": t.succeeded}
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
