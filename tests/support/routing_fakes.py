"""In-fence test doubles for the routing track (no tests in this module).

The inference track builds the real backends in parallel; routing codes only
against the `interfaces.py` protocols, so these doubles are deliberately tiny:

* `FakeHashEmbedder` — deterministic 384-dim signed token-hash projection,
  L2-normalised. Lexical, not semantic: shared tokens => cosine similarity.
* `ScriptedDecisionModel` — answers Choice/Score/Noul from injectable
  callables and records every call, so tests can assert on what the pipeline
  asked (e.g. "domain choice was over these options").
* `ExplodingDecisionModel` — raises on every call (FR-06 fallback tests).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from mcprouter.interfaces import ChoiceResult, ScoreResult
from mcprouter.models import (
    EMBEDDING_DIM,
    MCPServerRecord,
    MCPToolRecord,
    SkillRecord,
    SkillSourceRecord,
)

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    # Split snake_case tool names too: "search_issues" -> search, issues.
    return _TOKEN.findall(text.lower().replace("_", " "))


class FakeHashEmbedder:
    name = "fake-hash-test"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * EMBEDDING_DIM
            for tok in _tokens(t):
                h = hashlib.sha256(tok.encode()).digest()
                idx = int.from_bytes(h[:4], "big") % EMBEDDING_DIM
                vec[idx] += 1.0 if h[4] & 1 else -1.0
            norm = math.sqrt(sum(v * v for v in vec))
            if norm == 0.0:
                vec[0] = 1.0  # never emit a zero vector (cosine undefined)
                norm = 1.0
            out.append([v / norm for v in vec])
        return out


def _uniform_choice(state: str, question: str, options: list[str]) -> ChoiceResult:
    p = 1.0 / len(options)
    return ChoiceResult(option=options[0], probabilities={o: p for o in options})


def _flat_score(state: str, question: str, levels: list[str]) -> ScoreResult:
    p = 1.0 / len(levels)
    return ScoreResult(level=0, probabilities=[p] * len(levels))


@dataclass
class ScriptedDecisionModel:
    """Every answer comes from a callable; defaults are neutral."""

    choice_fn: Callable[[str, str, list[str]], ChoiceResult] = _uniform_choice
    score_fn: Callable[[str, str, list[str]], ScoreResult] = _flat_score
    noul_fn: Callable[[str, str], float] = lambda state, question: 0.9
    name: str = "scripted-test"
    calls: list[tuple[str, str, str, tuple[str, ...]]] = field(default_factory=list)

    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        self.calls.append(("choice", state, question, tuple(options)))
        res = self.choice_fn(state, question, options)
        assert res.option in options, "double must honour the protocol"
        return res

    def score(self, state: str, question: str, levels: list[str]) -> ScoreResult:
        self.calls.append(("score", state, question, tuple(levels)))
        return self.score_fn(state, question, levels)

    def noul(self, state: str, question: str) -> float:
        self.calls.append(("noul", state, question, ()))
        return self.noul_fn(state, question)


class ModelDown(RuntimeError):
    pass


class ExplodingDecisionModel:
    name = "exploding-test"

    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        raise ModelDown("decision model unavailable")

    def score(self, state: str, question: str, levels: list[str]) -> ScoreResult:
        raise ModelDown("decision model unavailable")

    def noul(self, state: str, question: str) -> float:
        raise ModelDown("decision model unavailable")


def add_server(s: Session, name: str, **kw: object) -> MCPServerRecord:
    srv = MCPServerRecord(name=name, transport="stdio", status=kw.pop("status", "healthy"), **kw)
    s.add(srv)
    s.flush()
    return srv


def add_tool(
    s: Session,
    server: MCPServerRecord,
    name: str,
    description: str,
    *,
    embedder: FakeHashEmbedder | None,
    domain: str | None = None,
    operation: str = "read",
    tags: list[str] | None = None,
    **kw: object,
) -> MCPToolRecord:
    tool = MCPToolRecord(
        server_id=server.id,
        name=name,
        description=description,
        schema_hash="0" * 64,
        domain=domain,
        operation=operation,
        tags=tags or [],
        **kw,
    )
    if embedder is not None:
        tool.embedding = embedder.embed([f"{name} {description} {' '.join(tags or [])}"])[0]
        tool.embedding_backend = embedder.name
    s.add(tool)
    s.flush()
    return tool


def pick(option: str, p: float = 0.9) -> Callable[[str, str, list[str]], ChoiceResult]:
    """Choice fn that picks `option` when offered, else the first option."""

    def fn(state: str, question: str, options: list[str]) -> ChoiceResult:
        chosen = option if option in options else options[0]
        rest = (1.0 - p) / max(1, len(options) - 1)
        probs = {o: (p if o == chosen else rest) for o in options}
        return ChoiceResult(option=chosen, probabilities=probs)

    return fn


def add_skill(
    s: Session, name: str, description: str, *, embedder: FakeHashEmbedder, op: str = "read"
) -> SkillRecord:
    """S2d: an eligible, embedded skill under its own directory source."""
    src = SkillSourceRecord(name=f"src-{name}", kind="directory", location=f"/x/{name}")
    s.add(src)
    s.flush()
    sk = SkillRecord(
        source_id=src.id, name=name, description=description, relative_path=name,
        content_hash="0" * 64, manifest_hash="0" * 64, operation=op,
        classification_reviewed=True, classification_source="human", ingest_flags=[],
    )  # fmt: skip
    sk.embedding = embedder.embed([f"{name} {description}"])[0]
    sk.embedding_backend = embedder.name
    s.add(sk)
    s.flush()
    return sk
