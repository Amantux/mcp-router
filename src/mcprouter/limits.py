"""Rate-limiter registry (D10, P-606): ONE per app, on ``app.state.limiters``.

Keyed ``(surface, agent)``; every surface has its own per-agent budget per
60 s sliding window:

=========== ========================================= =====================
surface     who consumes it                           budget (per minute)
=========== ========================================= =====================
execute     ExecutionManager (REST + MCP tool calls)  rate_limit_per_agent_per_min
skills      SkillExposure (activate / bundle)         rate_limit_per_agent_per_min
route       MCP find_tools                            rate_limit_per_agent_per_min
decision    System One decision edge                  decision_rate_limit_per_min
feedback    route feedback (REST + MCP)               30 (FEEDBACK_LIMIT_PER_MIN)
=========== ========================================= =====================

Shared-budget semantics (documented on purpose): budgets are PER SURFACE,
not pooled. An agent may spend its full `execute` budget and its full
`route` budget in the same minute; REST and MCP calls on one surface share
that surface's budget (one registry per app, one key per agent). Two apps in
one process never share anything: there is no module-global state here.

Idle keys are pruned (every PRUNE_EVERY acquisitions) so a churn of agent ids
cannot grow memory without bound.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping

from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.settings import Settings

WINDOW_S = 60.0
FEEDBACK_LIMIT_PER_MIN = 30
PRUNE_EVERY = 1024
SURFACES = ("execute", "skills", "route", "decision", "feedback")

MIB = 1024 * 1024
#: The one request-body cap: the app-wide middleware (api.body_limit) and the
#: MCP SDK's own /mcp cap (gateway.server) both use it.
MAX_BODY_BYTES = MIB


def format_bytes(n: int) -> str:
    """Human size for cap error texts ("1 MiB"), built from the cap itself so
    a message can never disagree with the limit it reports."""
    return f"{n / MIB:g} MiB"


class SurfaceLimiter:
    """One surface of a registry, shaped like SlidingWindowLimiter
    (`try_acquire(key)`, `limit`) so existing components take it unchanged."""

    def __init__(self, registry: LimiterRegistry, surface: str) -> None:
        registry.budget(surface)  # unknown surface fails here, at wiring time
        self._registry = registry
        self.surface = surface

    @property
    def limit(self) -> int:
        return self._registry.budget(self.surface)

    def try_acquire(self, key: str) -> bool:
        return self._registry.try_acquire(self.surface, key)


class LimiterRegistry:
    def __init__(
        self,
        budgets: Mapping[str, int],
        *,
        window_s: float = WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
        prune_every: int = PRUNE_EVERY,
    ) -> None:
        self._budgets = dict(budgets)
        self._window_s = window_s
        self._clock = clock
        self._prune_every = prune_every
        self._limiters: dict[tuple[str, str], SlidingWindowLimiter] = {}
        self._lock = threading.Lock()
        self._calls = 0

    def budget(self, surface: str) -> int:
        try:
            return self._budgets[surface]
        except KeyError:
            raise KeyError(f"unknown rate-limit surface {surface!r}") from None

    def get(self, surface: str, agent: str) -> SlidingWindowLimiter:
        """The limiter dedicated to (surface, agent); created on first use."""
        limit = self.budget(surface)
        with self._lock:
            lim = self._limiters.get((surface, agent))
            if lim is None:
                lim = SlidingWindowLimiter(limit, self._window_s, clock=self._clock)
                self._limiters[(surface, agent)] = lim
            return lim

    def try_acquire(self, surface: str, agent: str) -> bool:
        ok = self.get(surface, agent).try_acquire(agent)
        with self._lock:
            self._calls += 1
            due = self._calls % self._prune_every == 0
        if due:
            self.prune()
        return ok

    def surface(self, name: str) -> SurfaceLimiter:
        return SurfaceLimiter(self, name)

    def prune(self) -> int:
        """Drop (surface, agent) entries with no hit inside the window."""
        with self._lock:
            idle = [k for k, lim in self._limiters.items() if lim.is_idle()]
            for k in idle:
                del self._limiters[k]
        return len(idle)

    def __len__(self) -> int:
        with self._lock:
            return len(self._limiters)


def make_limiters(
    settings: Settings, *, clock: Callable[[], float] = time.monotonic
) -> LimiterRegistry:
    per_agent = settings.rate_limit_per_agent_per_min
    return LimiterRegistry(
        {
            "execute": per_agent,
            "skills": per_agent,
            "route": per_agent,
            "decision": settings.decision_rate_limit_per_min,
            "feedback": FEEDBACK_LIMIT_PER_MIN,
        },
        clock=clock,
    )
