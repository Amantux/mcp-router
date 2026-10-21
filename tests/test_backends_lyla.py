"""The lyla decision backend (MCPR_DECISION_BACKEND=lyla).

Covers: registration & engine loading (via the engine's decision-loader seam),
the catalog-adapted choice()/score()/noul() protocol shapes under
ValidatedDecisionModel, fallback semantics (curated ModelUnavailableError ->
deterministic-v1; unknown backend names remain configuration errors), the
generation-driven index rebuild, and ports of lyla's own degraded-metadata
regressions (blank description / tags) at the adapter level. No database.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from mcprouter.inference.engine import DECISION_BACKENDS, InferenceEngine, default_decision_loader
from mcprouter.inference.errors import ModelUnavailableError
from mcprouter.inference.hash_backend import HashEmbeddingBackend
from mcprouter.inference.lyla_backend import (
    CatalogRow,
    LylaDecisionModel,
    StaticCatalogSource,
    build_lyla_decision_model,
    row_to_entry,
    success_rate,
)
from mcprouter.inference.validation import ValidatedDecisionModel
from mcprouter.interfaces import (
    BatchScoringDecisionModel,
    DecisionModel,
    EmbeddingBackend,
    ScoreResult,
)
from mcprouter.lyla import DecisionModel as LylaPipeline
from mcprouter.lyla import Query
from mcprouter.routing.pipeline import (
    DOMAIN_QUESTION,
    OPERATION_QUESTION,
    RELEVANCE_LEVELS,
    SCORE_QUESTION,
)
from mcprouter.settings import DECISION_BACKEND_CHOICES, Settings

TASK = "create an issue in the github repository"
PDF_TASK = "merge multiple pdf files into one document"


# ----------------------------------------------------------------- fixtures
def _tool(
    row_id: str,
    name: str,
    container: str,
    description: str,
    domain: str,
    capabilities: tuple[str, ...],
    *,
    operation: str = "read",
    latency: float | None = 80.0,
    calls: int = 10,
    errors: int = 0,
) -> CatalogRow:
    return CatalogRow(
        kind="tool",
        id=row_id,
        name=name,
        container_name=container,
        description=description,
        domain=domain,
        capabilities=capabilities,
        tags=(),
        operation=operation,
        enabled=True,
        available=True,
        container_enabled=True,
        container_status="healthy",
        version=1,
        call_count=calls,
        error_count=errors,
        avg_latency_ms=latency,
        input_schema={"properties": {"q": {"type": "string"}}},
    )


def _synthetic_catalog() -> tuple[CatalogRow, ...]:
    """Six dev tools (issues/pulls) and six spreadsheet tools, two domains."""
    dev = tuple(
        _tool(
            f"dev{i}",
            f"github_tool_{i}",
            "github-server",
            "create, list and search github issues and pull requests",
            "development",
            ("github", "issue", "pull"),
            operation="write",
        )
        for i in range(6)
    )
    sheets = tuple(
        _tool(
            f"sheet{i}",
            f"sheet_tool_{i}",
            "sheets-server",
            "read and edit xlsx spreadsheet cells",
            "productivity",
            ("spreadsheet", "xlsx"),
        )
        for i in range(6)
    )
    return dev + sheets


def _model(rows: tuple[CatalogRow, ...], **kw: Any) -> LylaDecisionModel:
    model = LylaDecisionModel(HashEmbeddingBackend(), source=StaticCatalogSource(rows), **kw)
    model.warm_up()
    return model


def _tool_question(server: str, tool: str, description: str) -> str:
    """The router's per-candidate score question shape (routing/pipeline.py)."""
    return f"{SCORE_QUESTION}\nTool: {server}/{tool}\nDescription: {description}"


def _expected_level(result: ScoreResult) -> float:
    return sum(i * p for i, p in enumerate(result.probabilities))


# ------------------------------------------------------------- registration
def test_lyla_is_a_selectable_backend() -> None:
    assert "lyla" in DECISION_BACKENDS
    assert set(DECISION_BACKEND_CHOICES) == set(DECISION_BACKENDS)
    from_env = Settings.from_env({"MCPR_DECISION_BACKEND": "lyla"})
    assert from_env.decision_backend == "lyla"


def test_engine_loads_lyla_backend() -> None:
    """(a) selectable + loads through the engine loader (loader seam, zero-ML)."""
    seen: list[EmbeddingBackend] = []

    def loader(settings: Settings, device: str, embedder: EmbeddingBackend) -> DecisionModel:
        seen.append(embedder)
        return build_lyla_decision_model(
            settings, embedder, source=StaticCatalogSource(_synthetic_catalog())
        )

    eng = InferenceEngine(
        Settings(embedding_backend="hash", decision_backend="lyla"),
        decision_loader=loader,
    )
    eng.load()
    assert isinstance(seen[0], HashEmbeddingBackend)  # engine supplies a LOCAL embedder
    dm = eng.decision_model()
    assert dm.name.startswith("lyla@")
    assert isinstance(dm, DecisionModel) and isinstance(dm, BatchScoringDecisionModel)
    h = eng.health()
    assert h["decision"]["backend"] == dm.name
    assert h["decision"]["fallbackUsed"] is False
    assert h["versions"]["lyla"] == "013ff01"
    chosen = eng.choice(TASK, DOMAIN_QUESTION, ["productivity", "development"])
    assert chosen.option == "development"


def test_default_lyla_loader_curates_source_failures() -> None:
    """A catalog source failure becomes a curated ModelUnavailableError (the
    engine's fallback takes over); unknown backend names stay config errors."""

    class Broken:
        def token(self) -> int:
            return 1

        def rows(self) -> list[CatalogRow]:
            raise RuntimeError("db down")

    with pytest.raises(ModelUnavailableError, match=r"catalog unavailable \(RuntimeError\)"):
        build_lyla_decision_model(
            Settings(embedding_backend="hash", decision_backend="lyla"),
            HashEmbeddingBackend(),
            source=Broken(),
        )


def test_unknown_decision_backend_name_is_a_configuration_error() -> None:
    eng = InferenceEngine(Settings(decision_backend="lyla2"))
    with pytest.raises(ValueError, match="decision backend must be one of"):
        eng.load()


# ------------------------------------------------------------- choice mapping
def test_choice_argmax_is_lyla_top_ranked() -> None:
    """(b) the argmax option is the one containing lyla's top-ranked entry."""
    m = _model(_synthetic_catalog())
    result = m.choice(TASK, DOMAIN_QUESTION, ["productivity", "development"])
    assert result.option == "development"
    assert set(result.probabilities) == {"productivity", "development"}
    assert result.probabilities["development"] > result.probabilities["productivity"]
    assert abs(sum(result.probabilities.values()) - 1.0) < 1e-9
    # lyla's own top-ranked entry on the adapted catalog sits in that domain.
    probe = LylaPipeline(m.lyla_index, k_coarse=100)
    decision = probe.decide(Query(intent=TASK))
    top_row = next(r for r in _synthetic_catalog() if r.id == decision.selection[0])
    assert top_row.domain == "development"


def test_choice_operation_axis() -> None:
    m = _model(_synthetic_catalog())
    result = m.choice("list the open issues in the repo", OPERATION_QUESTION, ["read", "write"])
    assert result.option == "read"
    result = m.choice("create an issue in the repo", OPERATION_QUESTION, ["read", "write"])
    assert result.option == "write"


def test_choice_unknown_question_ranks_options_lexically() -> None:
    m = _model(_synthetic_catalog())
    result = m.choice("merge some pdf files", "Which library owns this?", ["pdf tooling", "email"])
    assert result.option in ("pdf tooling", "email")


def test_choice_on_empty_catalog_stays_in_options() -> None:
    m = _model(())
    result = m.choice(TASK, DOMAIN_QUESTION, ["development", "productivity"])
    assert result.option in ("development", "productivity")
    assert abs(sum(result.probabilities.values()) - 1.0) < 1e-9


# ------------------------------------------------------- degraded metadata (c)
def test_blank_description_target_still_chosen() -> None:
    """Port of lyla's DegradedMetadata regressions: a target whose DESCRIPTION
    is blank (or whose capability tags are empty) must still win on structural
    features through the router adapter."""
    noise = tuple(
        _tool(
            f"noise{i}",
            f"spreadsheet_tool_{i}",
            "sheets-server",
            "edit spreadsheet files using tool variant",
            "productivity",
            ("spreadsheet", "edit"),
            operation="write",
        )
        for i in range(20)
    )
    # blank description, tags only
    blank_target = _tool("target:pdf", "merge_pdf_pages", "cal-server", "", "pdf",
                         ("pdf", "merge", "documents"), operation="write")  # fmt: skip
    m = _model(noise + (blank_target,))
    chosen = m.choice(
        "merge multiple pdf files into one document", DOMAIN_QUESTION, ["productivity", "pdf"]
    )
    assert chosen.option == "pdf", chosen.probabilities

    # empty capability tags, description carries the signal (lyla's variant)
    tagless_target = _tool("target:email", "send_email", "cal-server2",
                           "compose and send an email to one or more recipients", "email",
                           (), operation="write")  # fmt: skip
    m2 = _model(noise + (tagless_target,))
    chosen = m2.choice(
        "send an email to a client about the invoice", DOMAIN_QUESTION, ["productivity", "email"]
    )
    assert chosen.option == "email", chosen.probabilities


def test_blank_description_score_still_ranks_target() -> None:
    """The blank-description robustness holds on the score() path too: the
    matched blank-description entry outscores an unrelated tool."""
    noise = _tool("sheet0", "sheet_tool_0", "sheets-server",
                  "read and edit xlsx spreadsheet cells", "productivity", ("spreadsheet",))  # fmt: skip
    target = _tool("pdf0", "merge_pdf_pages", "cal-server", "", "pdf",
                   ("pdf", "merge", "documents"), operation="write")  # fmt: skip
    m = _model((noise, target))
    task = "merge multiple pdf files into one document"
    target_ev = sum(
        i * p
        for i, p in enumerate(
            m.score(
                task, _tool_question("cal-server", "merge_pdf_pages", ""), RELEVANCE_LEVELS
            ).probabilities
        )
    )
    noise_ev = sum(
        i * p
        for i, p in enumerate(
            m.score(
                task,
                _tool_question("sheets-server", "sheet_tool_0", noise.description),
                RELEVANCE_LEVELS,
            ).probabilities
        )
    )
    assert target_ev > noise_ev


# ------------------------------------------------------------------ shapes (e)
def test_score_and_noul_shapes_pass_validation() -> None:
    """(e) ScoreResult/score_batch/noul under ValidatedDecisionModel wrapping."""
    m = _model(_synthetic_catalog())
    wrap = ValidatedDecisionModel(m)
    scores = wrap.score_batch(
        "create a github issue",
        [
            _tool_question("github-server", "github_tool_0", "create issues"),
            _tool_question("sheets-server", "sheet_tool_0", "edit spreadsheet cells"),
        ],
        RELEVANCE_LEVELS,
    )
    assert len(scores) == 2
    for result in scores:
        assert result.level in range(len(RELEVANCE_LEVELS))
        assert abs(sum(result.probabilities) - 1.0) < 1e-3

    # the github question answers more relevant than the spreadsheet question
    def ev(result: object) -> float:
        return sum(i * p for i, p in enumerate(result.probabilities))  # type: ignore[attr-defined]

    assert ev(scores[0]) > ev(scores[1])
    single = wrap.score("edit a chart", _tool_question("sheets-server", "sheet_tool_1", "charts"),
                        RELEVANCE_LEVELS)  # fmt: skip
    assert single.level in range(len(RELEVANCE_LEVELS))
    noul = wrap.noul(
        "create a github issue", "Does any of these tools fit the task?\n- github-server/github_tool_0: create issues"
    )  # fmt: skip
    assert 0.0 <= noul <= 1.0


def test_noul_floor_semantics_match_deterministic_calibration() -> None:
    m = _model(_synthetic_catalog())
    fit = m.noul(
        "create a github issue",
        "Does any of these tools fit?\n- github-server/github_tool_0: create github issues",
    )
    unfit = m.noul(
        "buy a submarine and sail it to antarctica",
        "Does any of these tools fit?\n- github-server/github_tool_0: create github issues",
    )
    assert fit > 0.5  # route_confidence_floor (0.35) is passed
    assert unfit < 0.35  # below the floor: the route reports no_match


def test_model_satisfies_protocol() -> None:
    m = _model(_synthetic_catalog())
    assert isinstance(m, DecisionModel) and isinstance(m, BatchScoringDecisionModel)


# ----------------------------------------------------------------- fallback (d)
def test_unavailable_lyla_degrades_to_deterministic_v1() -> None:
    def loader(settings: Settings, device: str, embedder: EmbeddingBackend) -> DecisionModel:
        raise ModelUnavailableError("lyla backend: catalog unavailable (OperationalError)")

    eng = InferenceEngine(
        Settings(decision_backend="lyla"),
        decision_loader=loader,
    )
    eng.load()  # must not raise: the fallback takes over with the curated reason
    h = eng.health()["decision"]
    assert h["backend"] == "deterministic-v1"
    assert h["fallbackUsed"] is True
    assert h["fallbackReason"] == "lyla backend: catalog unavailable (OperationalError)"
    assert eng.choice("s", DOMAIN_QUESTION, ["a", "b"]).option == "a"


def test_default_loader_wiring_selects_lyla(monkeypatch: pytest.MonkeyPatch) -> None:
    """default_decision_loader dispatches on settings: lyla -> its builder."""
    calls: list[str] = []

    def fake_builder(settings: Settings, embedder: object) -> str:
        calls.append("lyla")
        return "model-sentinel"

    monkeypatch.setattr("mcprouter.inference.lyla_backend.build_lyla_decision_model", fake_builder)
    out: Any = default_decision_loader(
        Settings(decision_backend="lyla"), "cpu", HashEmbeddingBackend()
    )
    assert out == "model-sentinel" and calls == ["lyla"]


# ------------------------------------------------------- rebuild on generation
def test_index_rebuilds_when_generation_changes() -> None:
    source = StaticCatalogSource(_synthetic_catalog())
    m = LylaDecisionModel(HashEmbeddingBackend(), source=source)
    m.warm_up()
    assert m.catalog_size() == 12
    extra = _tool("late1", "late_tool", "late-server", "translate a document to english",
                  "development", ("translate", "language"))  # fmt: skip
    source.items = (*source.items, extra)
    source.token_value = 1  # generation bump -> the next call rebuilds
    m.warm_up()
    assert m.catalog_size() == 13
    assert m.lyla_index.get("late1") is not None
    source.items = tuple(r for r in source.items if r.id != "late1")
    source.token_value = 2
    m.refresh()
    assert m.lyla_index.get("late1") is None
    assert m.catalog_size() == 12


# ------------------------------------------------------------- row -> entry
def test_row_to_entry_mapping() -> None:
    row = CatalogRow(
        kind="skill",
        id="skill:abc",
        name="pdf_merge",
        container_name="docs-source",
        description="merge pdf files",
        domain="pdf",
        capabilities=("documents",),
        tags=("pdf",),
        operation="write",
        enabled=True,
        available=False,
        container_enabled=False,
        container_status="offline",
        version=3,
        body_tokens_est=5_000,
        input_schema={"properties": {"q": {"type": "string"}, "n": {"type": "integer"}}},
    )
    entry = row_to_entry(row)
    assert entry.id == "skill:abc" and entry.type == "skill"
    assert entry.capability_tags == ["documents", "pdf"]
    assert entry.domain_tags == ["pdf"]
    assert entry.input_types == ["integer", "string"]
    assert entry.output_types == ["text"]
    assert entry.cost_profile.token_cost_class == "high"
    assert entry.cost_profile.monetary_cost_class == "free"
    assert entry.availability.enabled is False  # disabled source
    assert entry.availability.connected is False  # offline source
    assert entry.entry_version == 3
    assert entry.extra["operation"] == "write"
    assert success_rate(replace(row, call_count=10, error_count=2)) == 0.8
    assert success_rate(replace(row, call_count=0)) == 1.0


def test_settings_knobs_reach_the_model() -> None:
    settings = Settings(embedding_backend="hash", decision_backend="lyla")
    settings = replace(settings, lyla_k_coarse=7, lyla_epsilon=0.2, lyla_min_confidence=0.9)
    m = build_lyla_decision_model(
        settings, HashEmbeddingBackend(), source=StaticCatalogSource(_synthetic_catalog())
    )
    assert (m._k_coarse, m._epsilon, m._min_confidence) == (7, 0.2, 0.9)
