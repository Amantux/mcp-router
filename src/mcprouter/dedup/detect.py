"""Pairwise duplicate detection over same-domain tools.

Score (all components in [0, 1]):
    combined = 0.6 * embedding cosine
             + 0.2 * name-token Jaccard
             + 0.2 * input-schema property-name Jaccard
A pair becomes a suggestion when combined >= threshold (default 0.85).

Candidate pairs (computed in SQL with pgvector, so we never pull 1,000
vectors into Python):
* same non-null `domain`;
* both have an embedding from the SAME `embedding_backend` (vectors from
  different backends are never compared — scoping §5); pairs lacking an
  embedding are skipped, as are zero vectors (cosine undefined);
* compatible execution behaviour: same `operation`, or either is `unknown`;
* cosine >= (threshold - 0.4) / 0.6 — the most the two Jaccard terms can add
  is 0.4, so this pre-filter is exact, not lossy.

Two tools with NO input properties count as Jaccard 1.0 (identical input
shapes). Pairs are stored normalised (tool_a_id < tool_b_id).

Re-runs: an existing OPEN suggestion for the pair is refreshed (score,
rationale, preferred tool); an accepted/dismissed pair is never re-suggested
— a human decision is not re-litigated on every run.

Requires `registry.schema.init_registry()` (unique pair index for
INSERT ... ON CONFLICT DO NOTHING).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from mcprouter.models import DuplicateSuggestion, MCPServerRecord, MCPToolRecord, utcnow
from mcprouter.registry.classify import split_identifier
from mcprouter.registry.errors import InvalidArgument
from mcprouter.registry.wire import success_rate

W_COSINE = 0.6
W_NAME = 0.2
W_SCHEMA = 0.2
DEFAULT_THRESHOLD = 0.85
MIN_THRESHOLD = 0.5

# Preferred-tool evidence floors.
MIN_CALLS = 10  # below this, rates/latency are noise
MIN_SUCCESS_DELTA = 0.05
MAX_LATENCY_RATIO = 0.8  # faster must be <= 80% of slower

_CANDIDATES_SQL = text(
    """
    SELECT a.id AS a_id, b.id AS b_id, 1 - (a.embedding <=> b.embedding) AS cosine
    FROM mcp_tools a
    JOIN mcp_tools b
      ON a.domain = b.domain
     AND a.id < b.id
     AND a.embedding_backend = b.embedding_backend
    WHERE a.domain IS NOT NULL
      AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
      AND vector_norm(a.embedding) > 0 AND vector_norm(b.embedding) > 0
      AND (a.operation = b.operation OR a.operation = 'unknown' OR b.operation = 'unknown')
      AND 1 - (a.embedding <=> b.embedding) >= :cos_floor
    """
)


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _props(schema: dict[str, Any] | None) -> set[str]:
    props = (schema or {}).get("properties")
    return {str(k).lower() for k in props} if isinstance(props, dict) else set()


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def preferred_tool(a: MCPToolRecord, b: MCPToolRecord) -> tuple[str | None, str]:
    """Pick the tool to prefer, in priority order: higher success rate, lower
    avg latency, narrower required scopes. (Admin preference: future.) Returns
    (None, "") when the evidence does not clearly favour either."""
    if a.call_count >= MIN_CALLS and b.call_count >= MIN_CALLS:
        ra, rb = success_rate(a), success_rate(b)
        assert ra is not None and rb is not None
        if abs(ra - rb) >= MIN_SUCCESS_DELTA:
            win, lose, rw, rl = (a, b, ra, rb) if ra > rb else (b, a, rb, ra)
            return win.id, (
                f"higher success rate ({_pct(rw)} vs {_pct(rl)} over "
                f"{win.call_count}/{lose.call_count} calls)"
            )
        la, lb = a.avg_latency_ms, b.avg_latency_ms
        if la is not None and lb is not None and max(la, lb) > 0:
            fast, slow, lf, ls = (a, b, la, lb) if la < lb else (b, a, lb, la)
            if lf <= MAX_LATENCY_RATIO * ls:
                return fast.id, f"lower avg latency ({lf:.0f}ms vs {ls:.0f}ms)"
    sa, sb = set(a.required_scopes or []), set(b.required_scopes or [])
    if sa < sb:
        return a.id, f"narrower required scopes ({len(sa)} vs {len(sb)})"
    if sb < sa:
        return b.id, f"narrower required scopes ({len(sb)} vs {len(sa)})"
    return None, ""


@dataclass(frozen=True)
class PairScore:
    combined: float
    rationale: str
    preferred_tool_id: str | None


def score_pair(
    a: MCPToolRecord, b: MCPToolRecord, cosine: float, names: dict[str, str]
) -> PairScore:
    ta, tb = set(split_identifier(a.name)), set(split_identifier(b.name))
    pa, pb = _props(a.input_schema), _props(b.input_schema)
    nj, sj = jaccard(ta, tb), jaccard(pa, pb)
    combined = W_COSINE * cosine + W_NAME * nj + W_SCHEMA * sj
    parts = [
        f"embedding cosine {cosine:.2f} ({a.embedding_backend})",
        f"name tokens Jaccard {nj:.2f} (shared: {', '.join(sorted(ta & tb)) or 'none'})",
        f"input properties Jaccard {sj:.2f} (shared: {', '.join(sorted(pa & pb)) or 'none'})",
        f"same domain '{a.domain}', operations {a.operation}/{b.operation}",
        f"combined {combined:.2f} = {W_COSINE}*cos + {W_NAME}*name + {W_SCHEMA}*schema",
    ]
    pid, why = preferred_tool(a, b)
    if pid is not None:
        parts.append(f"prefer {names.get(pid, pid)}: {why}")
    else:
        parts.append("no preferred tool: insufficient evidence")
    return PairScore(combined=combined, rationale="; ".join(parts) + ".", preferred_tool_id=pid)


@dataclass(frozen=True)
class DedupRun:
    pairs_considered: int
    created: int
    refreshed: int
    skipped_decided: int


def run_dedup(session: Session, *, threshold: float = DEFAULT_THRESHOLD) -> DedupRun:
    if not MIN_THRESHOLD <= threshold <= 1.0:
        raise InvalidArgument(f"threshold must be between {MIN_THRESHOLD} and 1.0.")
    cos_floor = (threshold - (W_NAME + W_SCHEMA)) / W_COSINE
    rows = session.execute(_CANDIDATES_SQL, {"cos_floor": cos_floor}).all()
    if not rows:
        return DedupRun(0, 0, 0, 0)

    ids = {r.a_id for r in rows} | {r.b_id for r in rows}
    tools = {
        t.id: t for t in session.scalars(select(MCPToolRecord).where(MCPToolRecord.id.in_(ids)))
    }
    server_names: dict[str, str] = {
        sid: sname
        for sid, sname in session.execute(
            select(MCPServerRecord.id, MCPServerRecord.name).where(
                MCPServerRecord.id.in_({t.server_id for t in tools.values()})
            )
        ).all()
    }
    labels = {tid: f"{server_names.get(t.server_id, '?')}/{t.name}" for tid, t in tools.items()}
    existing = {
        (s.tool_a_id, s.tool_b_id): s
        for s in session.scalars(
            select(DuplicateSuggestion).where(DuplicateSuggestion.tool_a_id.in_(ids))
        )
    }

    created = refreshed = decided = 0
    for r in rows:
        score = score_pair(tools[r.a_id], tools[r.b_id], float(r.cosine), labels)
        if score.combined < threshold:
            continue
        prior = existing.get((r.a_id, r.b_id))
        if prior is not None:
            if prior.status == "open":
                prior.similarity = score.combined
                prior.rationale = score.rationale
                prior.preferred_tool_id = score.preferred_tool_id
                refreshed += 1
            else:
                decided += 1
            continue
        res = session.execute(
            insert(DuplicateSuggestion)
            .values(
                id=str(uuid.uuid4()),
                tool_a_id=r.a_id,
                tool_b_id=r.b_id,
                similarity=score.combined,
                rationale=score.rationale,
                preferred_tool_id=score.preferred_tool_id,
                status="open",
                created_at=utcnow(),
            )
            .on_conflict_do_nothing(index_elements=["tool_a_id", "tool_b_id"])
            .returning(DuplicateSuggestion.id)
        )
        # RETURNING (not rowcount: ORM-enabled insert reports -1) — no row
        # back means a concurrent run inserted this pair first.
        created += int(res.scalar_one_or_none() is not None)
    session.flush()
    return DedupRun(
        pairs_considered=len(rows), created=created, refreshed=refreshed, skipped_decided=decided
    )
