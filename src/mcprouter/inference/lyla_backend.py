"""Adapter: the vendored Lyla decision model -> the router's DecisionModel protocol.

Lyla is vendored verbatim at `mcprouter.lyla` (Amantux/lyla @ 013ff01, MIT,
stdlib-only). Its pipeline is a CATALOG ranking engine — hard filter, coarse
retrieval (ANN + lexical, RRF), fine scoring, tie-break/abstain — over
`CatalogIndex` entries, while the router's DecisionModel protocol
(interfaces.py) is string-in/string-out: `choice(state, question, options)`
asks about DOMAINS/OPERATIONS, `score`/`score_batch` about RELEVANCE LEVELS,
`noul` about "does anything fit?". The adapter bridges the two:

* Catalog adaptation: the router's tool+skill catalog (models.py) is mapped
  into lyla `CatalogEntry` rows — descriptions, capability/domain tags,
  operation, I/O types, latency/token/monetary cost classes, success-rate and
  connectivity metadata (`availability`, `reliability`, `cost_profile`). The
  adapted index is built once (eagerly by the loader) and rebuilt whenever the
  router's `catalog_generation()` changes — the same invalidation signal the
  route cache uses; `DbCatalogSource` reports it as the token. Rebuilds are
  prepared off to the side (unchanged rows reuse their stored vectors; only
  changed rows are re-embedded, in batches) and swapped in atomically, so a
  concurrent decision never sees a half-updated catalog.
* Embeddings: the engine's LOCAL `EmbeddingBackend` is exposed to lyla through
  `RouterEmbedder` (lyla's embedder protocol: `embed(text) -> SparseVector`,
  `cosine`) instead of lyla's internal hashing embedder. Any other (remote
  aoai) embedder is substituted with the hash projection — the engine's
  zero-ML local backend — so no network call ever lands in the decision path
  (the same rule the deterministic decider applies).
* Protocol mapping — every path calls lyla's `DecisionModel.decide()`:
  - `choice(state, question, options)`: for each option, ONE `decide()` over
    the adapted catalog with `Environment(allowlist=<the option's members>)`;
    lyla's confidence for that option's pool becomes the option's score
    (softmax -> ChoiceResult probabilities; the argmax is the option whose
    best member lyla ranks highest). Recognised questions are the router's
    fixed domain/operation contract questions (routing/pipeline.py); any
    other shape ranks synthetic per-option entries built from the option
    strings themselves. Equal scores resolve to the FIRST option (stable,
    like deterministic-v1).
  - `score`/`score_batch(state, question[:], levels)`: the router's
    per-candidate question text is resolved back to its catalog row (the
    "Tool:"/"Skill: <container>/<name>" line of routing/pipeline.py's
    question shape); that row's entry — metadata included — is scored in a
    per-call mini catalog via ONE `decide()` per question with an allowlist,
    so the answer is THAT candidate's score, not the catalog's global winner.
    Questions that match no row are scored on a synthetic entry built from
    the question text itself. The score is then mapped onto the ordered level
    axis with the same discretised-Gaussian deterministic-v1 uses.
  - `noul(state, question)`: lyla's abstention, inverted. lyla abstains on an
    absolute confidence floor; the router's no-match question is per-line
    ("does any of these tools fit?"), so the abstention probability is
    carried by the signal that floor measures — semantic fit, line-wise —
    with the exact deterministic-v1 calibration (an embedded
    `DeterministicDecisionModel` over this backend's embedder). The returned
    value is 1 - P(abstain): the probability that something fits, on the same
    scale as deterministic-v1, so `route_confidence_floor` keeps its meaning
    across backends.

lyla's per-query shortlist cache is DISABLED here (ttl 0): the router already
caches whole routes (routing/cache.py), and lyla's cache key does not include
the `Environment`, so neighbouring allowlists within one choice() call could
cross-contaminate each other. Concurrency: rebuilds run under a lock; readers
without it see either the previous or the next full catalog, never a torn one.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from mcprouter.generation import catalog_generation
from mcprouter.inference.deterministic import DeterministicDecisionModel
from mcprouter.inference.errors import ModelUnavailableError
from mcprouter.interfaces import ChoiceResult, EmbeddingBackend, ScoreResult
from mcprouter.lyla import (
    Availability,
    CatalogEntry,
    CatalogIndex,
    CostProfile,
    Environment,
    Query,
    Reliability,
)
from mcprouter.lyla import (
    DecisionModel as LylaPipeline,
)
from mcprouter.lyla.text import SparseVector
from mcprouter.settings import Settings

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

log = logging.getLogger(__name__)

#: The vendored Lyla source this adapter is built against (see the provenance
#: header in mcprouter/lyla/__init__.py).surfaced by the engine's health output.
LYLA_COMMIT = "013ff01"
#: Upstream project identifier (provenance in health output; no Hub download).
LYLA_MODEL_ID = "Amantux/lyla"

_CHOICE_TEMPERATURE = 0.1  # softmax sharpness over option scores
_SCORE_SIGMA = 0.6  # discretised-Gaussian width over the level axis
_LATENCY_FAST_MS = 150.0
_LATENCY_SLOW_MS = 1000.0
_TOKEN_LOW = 1_000
_TOKEN_MEDIUM = 4_000
_EMBED_CHUNK = 64
_DB_CONNECT_TIMEOUT_S = 5.0
_REF_RE = re.compile(r"(?m)^(Tool|Skill):\s*(\S.*)$")
_WORD_RE = re.compile(r"[a-z0-9]+")


# --------------------------------------------------------------------- source
@dataclass(frozen=True)
class CatalogRow:
    """One routable catalog item — the plain-data input for a lyla entry.

    `id` is the routing fusion key (bare tool id; "skill:<id>" for skills, the
    models.SKILL_ID_PREFIX convention), `container_name` the MCP server name
    (tools) or skill source name (skills).
    """

    kind: str  # "tool" | "skill"
    id: str
    name: str
    container_name: str
    description: str
    domain: str | None
    capabilities: tuple[str, ...]
    tags: tuple[str, ...]
    operation: str
    enabled: bool
    available: bool
    container_enabled: bool
    container_status: str
    version: int
    call_count: int = 0
    error_count: int = 0
    avg_latency_ms: float | None = None
    body_tokens_est: int = 0
    input_schema: Mapping[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        """Hash over every field lyla consumes; stable across refreshes."""
        props = self.input_schema.get("properties") if isinstance(self.input_schema, dict) else None
        payload = (
            self.kind,
            self.id,
            self.name,
            self.container_name,
            self.description,
            self.domain,
            self.capabilities,
            self.tags,
            self.operation,
            self.enabled,
            self.available,
            self.container_enabled,
            self.container_status,
            self.version,
            None if self.avg_latency_ms is None else round(self.avg_latency_ms, 6),
            self.call_count,
            self.error_count,
            self.body_tokens_est,
            tuple(sorted(props)) if isinstance(props, dict) else (),
        )
        return hashlib.sha256(repr(payload).encode()).hexdigest()


@runtime_checkable
class CatalogSource(Protocol):
    """Where the adapted catalog comes from. `token()` must be cheap (no row
    fetch) and change exactly when the catalog could have: every catalog-
    affecting write bumps the router's generation counter, which is what
    `DbCatalogSource` reports."""

    def token(self) -> int: ...

    def rows(self) -> list[CatalogRow]: ...


@dataclass
class StaticCatalogSource:
    """In-memory source (tests; callers that already own their rows)."""

    items: tuple[CatalogRow, ...]
    token_value: int = 0

    def token(self) -> int:
        return self.token_value

    def rows(self) -> list[CatalogRow]:
        return list(self.items)


class DbCatalogSource:
    """Catalog rows straight from the router's tables (tools + skills).

    Returns ALL rows, not only the routing-eligible ones: enabled/available and
    server health are mapped into the entry's `availability`, so lyla's hard
    filter (not SQL) decides what is scoreable. Skills keep `reliability`
    neutral — the router records skill activations, not failures.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    def token(self) -> int:
        return catalog_generation()

    def rows(self) -> list[CatalogRow]:
        from sqlalchemy import select

        from mcprouter.models import MCPServerRecord, MCPToolRecord, SkillRecord, SkillSourceRecord

        tool_cols = (
            MCPToolRecord.id,
            MCPToolRecord.name,
            MCPServerRecord.name,
            MCPToolRecord.description,
            MCPToolRecord.domain,
            MCPToolRecord.capabilities,
            MCPToolRecord.tags,
            MCPToolRecord.operation,
            MCPToolRecord.enabled,
            MCPToolRecord.available,
            MCPServerRecord.enabled,
            MCPServerRecord.status,
            MCPToolRecord.version,
            MCPToolRecord.call_count,
            MCPToolRecord.error_count,
            MCPToolRecord.avg_latency_ms,
            MCPToolRecord.input_schema,
        )
        skill_cols = (
            SkillRecord.id,
            SkillRecord.name,
            SkillSourceRecord.name,
            SkillRecord.description,
            SkillRecord.domain,
            SkillRecord.capabilities,
            SkillRecord.tags,
            SkillRecord.operation,
            SkillRecord.enabled,
            SkillRecord.available,
            SkillSourceRecord.enabled,
            SkillSourceRecord.status,
            SkillRecord.version,
            SkillRecord.body_tokens_est,
        )
        with self._factory() as s:
            tool_rows = s.execute(
                select(*tool_cols)
                .join(MCPServerRecord, MCPServerRecord.id == MCPToolRecord.server_id)
                .order_by(MCPServerRecord.name, MCPToolRecord.name, MCPToolRecord.id)
            ).all()
            skill_rows = s.execute(
                select(*skill_cols)
                .join(SkillSourceRecord, SkillSourceRecord.id == SkillRecord.source_id)
                .order_by(SkillSourceRecord.name, SkillRecord.name, SkillRecord.id)
            ).all()
        out = [
            CatalogRow(
                kind="tool",
                id=r[0],
                name=r[1] or "",
                container_name=r[2] or "",
                description=r[3] or "",
                domain=r[4],
                capabilities=tuple(r[5] or ()),
                tags=tuple(r[6] or ()),
                operation=r[7] or "unknown",
                enabled=bool(r[8]),
                available=bool(r[9]),
                container_enabled=bool(r[10]),
                container_status=r[11] or "unknown",
                version=int(r[12] or 1),
                call_count=int(r[13] or 0),
                error_count=int(r[14] or 0),
                avg_latency_ms=r[15],
                input_schema=r[16] if isinstance(r[16], dict) else {},
            )
            for r in tool_rows
        ]
        out += [
            CatalogRow(
                kind="skill",
                id=f"skill:{r[0]}",
                name=r[1] or "",
                container_name=r[2] or "",
                description=r[3] or "",
                domain=r[4],
                capabilities=tuple(r[5] or ()),
                tags=tuple(r[6] or ()),
                operation=r[7] or "unknown",
                enabled=bool(r[8]),
                available=bool(r[9]),
                container_enabled=bool(r[10]),
                container_status=r[11] or "unknown",
                version=int(r[12] or 1),
                body_tokens_est=int(r[13] or 0),
            )
            for r in skill_rows
        ]
        return out


# --------------------------------------------------------------------- mapping
def _latency_class(avg_latency_ms: float | None) -> str:
    if avg_latency_ms is None:
        return "fast"  # lyla's default for unknown latency
    if avg_latency_ms <= _LATENCY_FAST_MS:
        return "fast"
    return "medium" if avg_latency_ms <= _LATENCY_SLOW_MS else "slow"


def _token_cost_class(body_tokens_est: int) -> str:
    if body_tokens_est <= _TOKEN_LOW:
        return "low"
    return "medium" if body_tokens_est <= _TOKEN_MEDIUM else "high"


def _input_types(input_schema: Mapping[str, Any]) -> tuple[str, ...]:
    """Input types from the JSON property shapes (informational: the router's
    questions never set `required_output_types`, so lyla's io_fit stays
    permissive rather than filtering on I/O)."""
    props = input_schema.get("properties") if isinstance(input_schema, dict) else None
    if not isinstance(props, dict):
        return ()
    types = sorted(
        {
            str(p.get("type"))
            for p in (v for v in props.values() if isinstance(v, dict))
            if isinstance(p.get("type"), str)
        }
    )
    return tuple(types[:8])


def success_rate(row: CatalogRow) -> float:
    """Observed success rate; 1.0 (lyla's neutral default) without data."""
    if row.call_count <= 0:
        return 1.0
    return max(0.0, (row.call_count - row.error_count) / row.call_count)


def row_to_entry(row: CatalogRow, *, id: str | None = None) -> CatalogEntry:
    """CatalogRow -> lyla CatalogEntry (catalog_schema.md shape).

    Output types are the uniform ["text"] MCP default and monetary cost is
    always "free": the router's catalog carries neither typed outputs nor
    pricing data, so both features stay neutral in scoring instead of guessing.
    """
    return CatalogEntry(
        id=id if id is not None else row.id,
        type="skill" if row.kind == "skill" else "mcp_tool",
        name=row.name,
        description=row.description,
        capability_tags=[t for t in (*row.capabilities, *row.tags) if t],
        domain_tags=[row.domain] if row.domain else [],
        input_types=list(_input_types(row.input_schema)),
        output_types=["text"],
        cost_profile=CostProfile(
            latency_class=_latency_class(row.avg_latency_ms),
            p50_ms=row.avg_latency_ms if row.avg_latency_ms is not None else 50.0,
            p99_ms=row.avg_latency_ms * 4.0 if row.avg_latency_ms is not None else 200.0,
            token_cost_class=_token_cost_class(row.body_tokens_est),
            monetary_cost_class="free",
        ),
        reliability=Reliability(
            success_rate_30d=success_rate(row),
            invocations_30d=row.call_count,
        ),
        availability=Availability(
            requires_auth=False,  # authorization is policy-gated AFTER ranking (FR-07)
            connected=row.available and row.container_status != "offline",
            enabled=row.enabled and row.container_enabled,
        ),
        entry_version=row.version,
        # extra carries what scoring does not read but the adapter needs back:
        extra={  # the operation axis for choice(), and the container name.
            "operation": row.operation,
            "container": row.container_name,
            "kind": row.kind,
        },
    )


# ------------------------------------------------------------------ embeddings
class RouterEmbedder:
    """lyla's embedder protocol (`embed(text) -> SparseVector`, `cosine`) over
    the engine's EmbeddingBackend: the dense 384-dim vector is projected into
    lyla's sparse space. Signs are kept, so cosine values are numerically the
    engine backend's. A bounded memo deduplicates repeated query texts (each
    decision embeds its state several times)."""

    def __init__(self, backend: EmbeddingBackend, *, memo_size: int = 512) -> None:
        self.name = backend.name
        self._backend = backend
        self._memo: OrderedDict[str, SparseVector] = OrderedDict()
        self._memo_size = memo_size

    def embed(self, text: str) -> SparseVector:
        hit = self._memo.get(text)
        if hit is not None:
            self._memo.move_to_end(text)
            return hit
        vec = {i: float(w) for i, w in enumerate(self._backend.embed([text])[0]) if w}
        self._memo[text] = vec
        self._memo.move_to_end(text)
        while len(self._memo) > self._memo_size:
            self._memo.popitem(last=False)
        return vec

    def embed_many(self, texts: Sequence[str]) -> list[SparseVector]:
        """Batch projection (catalog rebuilds); one vector per text, in order."""
        dense = self._backend.embed(list(texts))
        if len(dense) != len(texts):
            raise ModelUnavailableError("lyla backend: embedding backend returned the wrong width")
        return [{i: float(w) for i, w in enumerate(vec) if w} for vec in dense]

    @staticmethod
    def cosine(a: SparseVector, b: SparseVector) -> float:
        if not a or not b:
            return 0.0
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b[k] for k, v in a.items() if k in b)


class _BatchedEmbedder:
    """Precomputed embeddings served as a fresh index's embedder during a
    rebuild: CatalogIndex.add() sees the exact embedding text it hashes
    (duplicates share one vector); anything else falls through to the live
    adapter, so decisions concurrent with the rebuild window are unaffected."""

    def __init__(self, vectors: dict[str, SparseVector], fallback: RouterEmbedder) -> None:
        self.name = fallback.name
        self._vectors = vectors
        self._fallback = fallback

    def embed(self, text: str) -> SparseVector:
        vec = self._vectors.get(text)
        return vec if vec is not None else self._fallback.embed(text)

    cosine = staticmethod(RouterEmbedder.cosine)


# --------------------------------------------------------------- small helpers
def _softmax(xs: list[float]) -> list[float]:
    """Softmax with a small floor: strictly positive, sums to 1 (the protocol
    wants a distribution over exactly the supplied options/levels)."""
    floor = 1e-4
    m = max(xs)
    ex = [max(2.718281828459045 ** (x - m), floor) for x in xs]
    total = sum(ex)
    return [e / total for e in ex]


def _argmax(xs: list[float]) -> int:
    """First maximal element (stable on ties, like deterministic-v1)."""
    best = 0
    for i, x in enumerate(xs):
        if x > xs[best]:
            best = i
    return best


def _levels_result(score: float, k: int) -> ScoreResult:
    """lyla confidence in [0, 1] -> ordered levels: a discretised Gaussian
    around score*(k-1), the same mapping deterministic-v1 gives its
    relevance. lyla's scores are calibrated around ~0.61 typical
    (heuristic_score), so the distributions center on level (k-1)*score."""
    k = max(k, 1)
    center = max(0.0, min(1.0, score)) * (k - 1)
    logits = [-((i - center) ** 2) / (2.0 * _SCORE_SIGMA**2) for i in range(k)]
    probs = _softmax(logits)
    return ScoreResult(level=_argmax(probs), probabilities=probs)


# ---------------------------------------------------------------- the backend
class LylaDecisionModel:
    """DecisionModel over the vendored lyla pipeline (name "lyla@<commit>")."""

    def __init__(
        self,
        embedder: EmbeddingBackend | None = None,
        *,
        source: CatalogSource,
        k_coarse: int = 100,
        epsilon: float = 0.03,
        min_confidence: float = 0.4,
    ) -> None:
        if k_coarse < 1:
            raise ValueError("k_coarse must be >= 1")
        if not 0.0 <= epsilon <= 1.0:
            raise ValueError("epsilon must be within [0, 1]")
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError("min_confidence must be within [0, 1]")
        self._source = source
        self._k_coarse = k_coarse
        self._epsilon = epsilon
        self._min_confidence = min_confidence
        self._router_embedder = RouterEmbedder(embedder) if embedder is not None else None
        self._index = CatalogIndex(embedder=self._router_embedder)
        # deterministic-v1's no-match calibration over THIS backend's embedder
        # (None: with a non-local embedder the no-match gate is lexical-only).
        self._no_match_gate = DeterministicDecisionModel(embedder=embedder)
        self._token: int | None = None
        self._fingerprints: dict[str, str] = {}
        self._rows_by_key: dict[tuple[str, str, str], CatalogRow] = {}
        self._rows_by_name: dict[tuple[str, str], CatalogRow] = {}
        self._refresh_lock = threading.Lock()
        # Recorded like the other backends (engine health output).
        self.model_id = LYLA_MODEL_ID
        self.revision = LYLA_COMMIT
        self.name = f"lyla@{LYLA_COMMIT}"

    # ------------------------------------------------------------- lifecycle
    @property
    def lyla_index(self) -> CatalogIndex:
        """The adapted lyla catalog (introspection, tests)."""
        return self._index

    def catalog_size(self) -> int:
        return len(self._index)

    def warm_up(self) -> None:
        """Build the adapted catalog now. The loader calls this at load time;
        failures surface as ModelUnavailableError and the engine degrades."""
        self._current()

    def refresh(self) -> None:
        """Force a full rebuild (normally generation-driven)."""
        self._current(force=True)

    def _current(self, *, force: bool = False) -> CatalogIndex:
        token = self._source.token()
        if not force and token == self._token:
            return self._index
        with self._refresh_lock:
            token = self._source.token()
            if force or self._token != token:
                self._rebuild(token)
        return self._index

    def _rebuild(self, token: int) -> None:
        t0 = time.perf_counter()
        try:
            rows = self._source.rows()
            index, fingerprints = self._fresh_index(rows)
        except Exception as exc:  # noqa: BLE001 — any snapshot/build failure degrades
            log.warning("lyla backend: catalog refresh failed (%s)", type(exc).__name__)
            raise ModelUnavailableError(
                f"lyla backend: catalog unavailable ({type(exc).__name__})"
            ) from exc
        # Atomic swap: decisions concurrent with this rebuild read either the
        # old or the new index (attribute assignment is the publication point).
        self._index = index
        self._fingerprints = fingerprints
        self._rows_by_key = {
            (row.kind, row.container_name, row.name): row
            for row in rows
            if row.kind in ("tool", "skill")
        }
        self._rows_by_name = {(r.container_name, r.name): r for r in rows}
        self._token = token
        log.info(
            "lyla backend: catalog refreshed (%d entries) in %.3fs",
            len(self._index),
            time.perf_counter() - t0,
        )

    def _fresh_index(self, rows: Sequence[CatalogRow]) -> tuple[CatalogIndex, dict[str, str]]:
        """Build the NEXT adapted index off to the side (the live state is
        untouched until the caller swaps it in). Unchanged rows reuse their
        stored vectors; changed rows are re-embedded in batches through the
        engine backend."""
        entries: list[CatalogEntry] = []
        fingerprints: dict[str, str] = {}
        for row in rows:
            entry = row_to_entry(row)
            entries.append(entry)
            fingerprints[row.id] = row.fingerprint()
        if self._router_embedder is None:
            index = CatalogIndex()  # lyla's internal hashing embedder embeds at add()
            index.add_many(entries)
            return index, fingerprints
        old_by_text: dict[str, SparseVector] = {
            entry.embedding_text(): vec
            for rid, entry in self._index.entries.items()
            if (vec := self._index.embeddings.get(rid)) is not None
        }
        vectors: dict[str, SparseVector] = dict(old_by_text)
        pending = [
            entry.embedding_text() for entry in entries if entry.embedding_text() not in vectors
        ]
        for start in range(0, len(pending), _EMBED_CHUNK):
            chunk = pending[start : start + _EMBED_CHUNK]
            for text, vec in zip(chunk, self._router_embedder.embed_many(chunk), strict=True):
                vectors[text] = vec
        index = CatalogIndex(embedder=_BatchedEmbedder(vectors, self._router_embedder))
        index.add_many(entries)
        index.embedder = self._router_embedder  # later decisions embed normally
        return index, fingerprints

    # ------------------------------------------------------------- pipeline
    def _pipeline(self, index: CatalogIndex) -> LylaPipeline:
        return LylaPipeline(
            index,
            k_coarse=self._k_coarse,
            epsilon=self._epsilon,
            min_confidence=self._min_confidence,
            cache_ttl_seconds=0.0,  # router route-cache owns reuse; lyla's key lacks the env
        )

    # -------------------------------------------------------------- protocol
    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        index = self._current()
        scores = self._option_scores(index, state, question, options)
        probs = _softmax([s / _CHOICE_TEMPERATURE for s in scores])
        best = _argmax(probs)
        return ChoiceResult(
            option=options[best],
            probabilities={o: p for o, p in zip(options, probs, strict=True)},
        )

    def _option_scores(
        self, index: CatalogIndex, state: str, question: str, options: list[str]
    ) -> list[float]:
        """One lyla decide() per option. Catalog-axis questions (the router's
        domain/operation contract) rank each option's member entries via an
        allowlist Environment; any other question shape ranks synthetic
        per-option entries built from the option strings themselves."""
        members = self._members_by_option(index, question, options)
        if all(not ids for ids in members.values()):
            index = self._synthetic_options_index(options, question)
        pipeline = self._pipeline(index)
        scores: list[float] = []
        for i, option in enumerate(options):
            ids = members.get(option) or {f"option:{i}"}
            decision = pipeline.decide(
                Query(intent=state, task_state=question),
                env=Environment(allowlist=set(ids)),
            )
            scores.append(decision.confidence)
        return scores

    def _synthetic_options_index(self, options: Sequence[str], question: str) -> CatalogIndex:
        """Options as one-off catalog entries (unknown question shapes): the
        option string is the name, the router's question text the description,
        the option's words the tags — lyla ranks them lexically/semantically."""
        index = CatalogIndex(embedder=self._router_embedder)
        for i, option in enumerate(options):
            index.add(
                CatalogEntry(
                    id=f"option:{i}",
                    name=option,
                    description=question,
                    capability_tags=_WORD_RE.findall(option.lower()),
                )
            )
        return index

    def _members_by_option(
        self, index: CatalogIndex, question: str, options: list[str]
    ) -> dict[str, set[str]]:
        axis = self._axis(question)
        known: dict[str, set[str]] = {o: set() for o in options}
        if axis is None:
            return known
        for entry_id, entry in index.entries.items():
            value: Any
            if axis == "domain":
                value = entry.domain_tags[0] if entry.domain_tags else None
            else:
                value = entry.extra.get("operation")
            if value in known:
                known[value].add(entry_id)
        return known

    @staticmethod
    def _axis(question: str) -> str | None:
        """The router's fixed question shapes (the decision-model contract at
        integration; routing/pipeline.py). Anything else is not a catalog axis
        and falls back to ranking the option strings."""
        from mcprouter.routing.pipeline import DOMAIN_QUESTION, OPERATION_QUESTION

        if question == DOMAIN_QUESTION:
            return "domain"
        if question == OPERATION_QUESTION:
            return "operation"
        return None

    def score(self, state: str, question: str, levels: list[str]) -> ScoreResult:
        return self.score_batch(state, [question], levels)[0]

    def score_batch(self, state: str, questions: list[str], levels: list[str]) -> list[ScoreResult]:
        """One lyla decide() per question on a per-call mini catalog holding
        exactly the asked-about candidate (scoped by an allowlist), so the
        confidence is THAT candidate's own score; then the discretised-Gaussian
        mapping onto the ordered level axis."""
        if not questions:
            return []
        k = max(len(levels), 1)
        self._current()  # rebuild when the generation changed; first build here
        results: list[ScoreResult] = []
        for i, question in enumerate(questions):
            query_id = f"question:{i}"
            local = CatalogIndex(embedder=self._router_embedder)
            local.add(row_to_entry(self._row_for(question), id=query_id))
            decision = self._pipeline(local).decide(
                Query(intent=state, task_state=question),
                env=Environment(allowlist={query_id}),
            )
            results.append(_levels_result(decision.confidence, k))
        return results

    def _row_for(self, question: str) -> CatalogRow:
        """Resolve the asked-about candidate: parse the router's "Tool:"/"Skill:"
        <container>/<name> question line against the adapted catalog rows
        (catalog metadata enters scoring); unmatched questions get a synthetic
        row built from the question text itself (blank-catalog robustness)."""
        match = _REF_RE.search(question)
        if match is not None:
            kind = "skill" if match.group(1) == "Skill" else "tool"
            ref = match.group(2).strip()
            row = self._match_ref(kind, ref) or self._match_ref_any(ref)
            if row is not None:
                return row
        return CatalogRow(
            kind="tool",
            id="",
            name="",
            container_name="",
            description=question,
            domain=None,
            capabilities=(),
            tags=(),
            operation="unknown",
            enabled=True,
            available=True,
            container_enabled=True,
            container_status="unknown",
            version=1,
        )

    def _match_ref(self, kind: str, ref: str) -> CatalogRow | None:
        """Every split point of "container/name" against the catalog rows."""
        parts = ref.split("/")
        for cut in range(1, len(parts)):
            row = self._rows_by_key.get((kind, "/".join(parts[:cut]), "/".join(parts[cut:])))
            if row is not None:
                return row
        return None

    def _match_ref_any(self, ref: str) -> CatalogRow | None:
        """Kind-agnostic fallback (a question whose kind marker misses)."""
        parts = ref.split("/")
        for cut in range(1, len(parts)):
            row = self._rows_by_name.get(("/".join(parts[:cut]), "/".join(parts[cut:])))
            if row is not None:
                return row
        return None

    def noul(self, state: str, question: str) -> float:
        """lyla's abstention, inverted: 1 - P(abstain) = the probability that
        something in the question fits the task (higher = more applicable), on
        deterministic-v1's calibrated scale (see module docstring)."""
        return self._no_match_gate.noul(state, question)


# ----------------------------------------------------------------- the loader
def default_lyla_source(settings: Settings) -> DbCatalogSource:
    """Session factory over the router's own database for the catalog source.

    `connect_timeout` bounds a down-DB startup instead of stalling the load.
    """
    from sqlalchemy import create_engine

    from mcprouter.db import make_session_factory

    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": _DB_CONNECT_TIMEOUT_S},
    )
    return DbCatalogSource(make_session_factory(engine))


def build_lyla_decision_model(
    settings: Settings,
    embedder: EmbeddingBackend | None,
    *,
    source: CatalogSource | None = None,
) -> LylaDecisionModel:
    """The engine's DecisionLoader for `MCPR_DECISION_BACKEND=lyla` (stdlib
    only; `device` is irrelevant — embeddings come from the engine's local
    embedder). Any construction/catalog failure is a curated
    ModelUnavailableError, so the engine degrades to deterministic-v1 with a
    reason; unknown backend names never reach this loader — they are the
    engine's configuration errors."""
    src = source if source is not None else default_lyla_source(settings)
    model = LylaDecisionModel(
        embedder,
        source=src,
        k_coarse=settings.lyla_k_coarse,
        epsilon=settings.lyla_epsilon,
        min_confidence=settings.lyla_min_confidence,
    )
    model.warm_up()
    return model
