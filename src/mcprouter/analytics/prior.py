"""Usage prior — a BOUNDED feedback signal the routing pipeline MAY consume.

NOT wired into routing (that is the routing track's call). OFF by default:
`Settings.usage_prior_enabled` <- `MCPR_USAGE_PRIOR_ENABLED`, parsed strictly
by settings.from_env (1/0, true/false, yes/no, on/off; empty string = unset;
anything else fails startup). This module never reads the environment.

    usage_prior = cap * selection_rate * confidence
    selection_rate = clamp(selected / surfaced, 0, 1)
    confidence     = min(1, log1p(surfaced) / log1p(SATURATION_SURFACED))

Properties (unit-tested): always in [0, cap]; a cold tool (surfaced == 0)
gets EXACTLY 0; disabled -> exactly 0; garbage inputs (negative, NaN,
selected > surfaced) can never push it outside the bounds. The log scale
means the first observations move it slowly, so a popular-early tool cannot
lock itself in (the rich-get-richer loop is capped by `cap`).

Feedback blend (wave 5): when a helpful_rate is supplied (None = no
feedback -> the formula above, unchanged),

    feedback_term = clamp((helpful_rate - 0.5) * 2, -1, 1)
    usage_prior   = clamp(cap * (W_SELECTION * rate * confidence
                                 + W_FEEDBACK * feedback_term), 0, cap)

W_SELECTION + W_FEEDBACK = 1, so the SAME cap bounds the blend; all-unhelpful
feedback can pull the prior to 0 but never below; a cold tool (surfaced == 0)
is still exactly 0 whatever its feedback; a NaN/inf helpful_rate is treated
as no feedback.

Intended use: an additive nudge on the blended [0, 1] routing score, small
enough (default cap 0.05) that it reorders near-ties but never overturns a
clear relevance gap.
"""

from __future__ import annotations

import math

DEFAULT_CAP = 0.05
SATURATION_SURFACED = 200
W_SELECTION = 0.7
W_FEEDBACK = 0.3


def usage_prior(
    selected: float,
    surfaced: float,
    *,
    enabled: bool,
    cap: float = DEFAULT_CAP,
    helpful_rate: float | None = None,
) -> float:
    if not enabled or not math.isfinite(cap) or cap <= 0:
        return 0.0
    if not (math.isfinite(selected) and math.isfinite(surfaced)) or surfaced <= 0:
        return 0.0
    rate = min(max(selected / surfaced, 0.0), 1.0)
    confidence = min(1.0, math.log1p(surfaced) / math.log1p(SATURATION_SURFACED))
    selection = rate * confidence
    if helpful_rate is None or not math.isfinite(helpful_rate):
        return min(cap, max(0.0, cap * selection))
    feedback = min(max((helpful_rate - 0.5) * 2.0, -1.0), 1.0)
    return min(cap, max(0.0, cap * (W_SELECTION * selection + W_FEEDBACK * feedback)))
