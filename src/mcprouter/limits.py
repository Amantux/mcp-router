"""Rate-limiter registry (wave-6 W0-3 seam; E6 fills it, D10).

One registry per app (``app.state.limiters``), keyed ``(surface, agent)`` with
per-surface budgets. Nothing consumes it yet; the existing per-component
limiters are unchanged."""

from __future__ import annotations

from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.settings import Settings


class LimiterRegistry:
    """Empty placeholder. ``get`` raises until E6 implements it, so an early
    consumer fails loudly instead of silently running unlimited."""

    def get(self, surface: str, agent: str) -> SlidingWindowLimiter:
        raise NotImplementedError("LimiterRegistry.get: not implemented yet (wave-6 E6)")


def make_limiters(settings: Settings) -> LimiterRegistry:
    del settings
    return LimiterRegistry()
