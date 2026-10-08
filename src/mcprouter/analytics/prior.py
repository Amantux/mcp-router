"""Usage prior — a BOUNDED feedback signal the routing pipeline MAY consume.

NOT wired into routing (that is the routing track's call). OFF by default:
`MCPR_USAGE_PRIOR_ENABLED` (truthy: 1/true/yes/on; empty string = unset).

    usage_prior = cap * selection_rate * confidence
    selection_rate = clamp(selected / surfaced, 0, 1)
    confidence     = min(1, log1p(surfaced) / log1p(SATURATION_SURFACED))

Properties (unit-tested): always in [0, cap]; a cold tool (surfaced == 0)
gets EXACTLY 0; disabled -> exactly 0; garbage inputs (negative, NaN,
selected > surfaced) can never push it outside the bounds. The log scale
means the first observations move it slowly, so a popular-early tool cannot
lock itself in (the rich-get-richer loop is capped by `cap`).

Intended use: an additive nudge on the blended [0, 1] routing score, small
enough (default cap 0.05) that it reorders near-ties but never overturns a
clear relevance gap.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping

DEFAULT_CAP = 0.05
SATURATION_SURFACED = 200
ENV_ENABLED = "MCPR_USAGE_PRIOR_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def prior_enabled(env: Mapping[str, str] | None = None) -> bool:
    raw = (os.environ if env is None else env).get(ENV_ENABLED, "")
    return raw.strip().lower() in _TRUTHY


def usage_prior(
    selected: float, surfaced: float, *, enabled: bool, cap: float = DEFAULT_CAP
) -> float:
    if not enabled or not math.isfinite(cap) or cap <= 0:
        return 0.0
    if not (math.isfinite(selected) and math.isfinite(surfaced)) or surfaced <= 0:
        return 0.0
    rate = min(max(selected / surfaced, 0.0), 1.0)
    confidence = min(1.0, math.log1p(surfaced) / math.log1p(SATURATION_SURFACED))
    return min(cap, max(0.0, cap * rate * confidence))
