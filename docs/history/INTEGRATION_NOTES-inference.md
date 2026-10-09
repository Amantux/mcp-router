# Integration notes — inference workstream (`feat/inference`)

Everything here is outside this branch's fence and has to land at integration.
Items are tagged **[needed]** (functionality depends on it) or
**[recommended]**.

## 1. Wiring into `api/app.py` [needed]

```python
from mcprouter.api.routes_models import router as models_router
from mcprouter.inference.engine import InferenceEngine

engine = InferenceEngine(settings)          # never loads at construction
app.state.inference_engine = engine         # routes_models reads exactly this
app.include_router(models_router)
# lifespan startup:  engine.load()          # SPEC §7 "load once at startup"
# lifespan shutdown: engine.unload()
```

* Build one engine per app: tests call `create_app()` several times with
  different settings, and production runs one app per process. The
  `get_process_engine()` singleton is for out-of-app code paths (CLI, catalog
  refresh jobs) that must share the app's engine. Don't use both in one
  process unless they get the same instance.
* Without `app.state.inference_engine`, `GET /api/v1/models/health` returns a
  curated `503 {"detail": "inference engine is not configured"}`.
* `engine.load()` with `bge`/`laya` takes several seconds (Laya ~4–7 s on
  CPU). Startup blocks for that long. If that's unacceptable, call it from a
  background thread; the first request loads lazily anyway. Loads run outside
  the engine's state lock, so health probes never block on a cold load.
* **[needed] Auth/side effects of the health route:** `routes_models.router`
  has no auth dependency (none exists yet on master). It exposes model ids,
  revisions, package versions and RSS: no secrets, but still operator
  information. Put it behind the same admin/API-key dependency as the other
  management routes. In battery mode the GET also performs the lazy idle
  check, which can unload models. That is intended (it is a "touch" point),
  but it means the probe has side effects.

## 2. Settings needs (`settings.py` is outside the fence) [recommended]

Engine constructor parameters already exist. Only the env plumbing is missing:

| env var | type / default | goes to |
|---|---|---|
| `MCPR_IDLE_UNLOAD_S` | float, 300 | `InferenceEngine(idle_unload_s=)` (battery mode only) |
| `MCPR_EMBED_BATCH_SIZE` | int, 32 | `InferenceEngine(embed_batch_size=)` |
| `MCPR_LAYA_NOUL_MODE` | `choice`\|`native`, `choice` | `LayaDecisionModel.load(noul_mode=)`, needs a custom `decision_loader` |

Existing settings are used as-is: `embedding_backend` (hash|bge),
`embedding_model_id`, `decision_backend` (deterministic|laya),
`laya_model_id`, `device` (auto|cpu|cuda|cuda:N), `operating_mode`,
`models_cache_dir`. **`models_cache_dir` semantics:** it is an HF_HOME-style
root, and the hub cache is `<models_cache_dir>/hub`. Set
`HF_HUB_OFFLINE=1` once the weights are cached, for offline operation (SPEC §3).

## 3. `pyproject.toml` needs [needed / recommended]

* **[needed]** Add `"laya==0.4.0"` to the `[inference]` extra. The Laya
  runtime is a pip package; the Hub repo only holds weights. Verified with
  0.4.0 (latest on PyPI, 2026-10-08).
* **[recommended]** Register the marker to silence
  `PytestUnknownMarkWarning`:
  `markers = ["slow: loads/downloads real models; set MCPR_RUN_SLOW=1"]`.
  The slow tests are already skipped unless `MCPR_RUN_SLOW=1`, so
  `pytest -q` stays fast either way.
* **[recommended]** Bring `bench/` under mypy (`files = ["src/mcprouter",
  "bench"]` or equivalent). It already passes `mypy --strict` (checked ad hoc
  with `mypy bench src/mcprouter`).
* mypy: **no config loosening needed.** Two targeted
  `# type: ignore[import-untyped]` in `inference/laya.py` (laya ships no
  `py.typed`). sentence-transformers and torch are typed.

## 4. Contract additions (`interfaces.py`, append-only)

* `BatchScoringDecisionModel(DecisionModel, Protocol)` with
  `score_batch(state, questions, levels) -> list[ScoreResult]`. Laya answers
  N questions in ONE forward pass. Measured on CPU: 8 questions batched take
  ~352 ms vs ~760 ms sequential, so candidate ranking must use it. Every
  engine-provided model implements it (deterministic loops; the validator
  falls back to looping for models that lack it).

New public surface in `mcprouter.inference` (no existing signature changed):

* `errors`: `InferenceError` > `ModelUnavailableError`,
  `EmbeddingDimensionError`, `EmbeddingRuntimeError`, `DecisionRuntimeError`,
  `DecisionProtocolError`. All messages are curated and safe at an API
  boundary.
* `engine.InferenceEngine`: `load/unload/set_mode/check_idle/health`,
  `embed/choice/score/score_batch/noul`, and three handles:
  `embedding_backend()`, `decision_model()`, `fallback_decision_model()`.
* `pipeline.embed_pending_tools(session, backend, *, batch_size=32) -> EmbedReport`,
  `canonical_tool_text`, `text_hash`.
* `validation.ValidatedDecisionModel`.

## 5. Contract for the routing workstream

* Get the model via `engine.decision_model()`. It is always wrapped in
  `ValidatedDecisionModel`: options are checked verbatim against the
  supplied list, the distribution is checked, and `DecisionProtocolError`
  is raised otherwise.
* **Fallback rule:** any `InferenceError` from it means "use
  `engine.fallback_decision_model()` (deterministic-v1) for this request and
  set `fallback_used=True`". `RoutingDecision.modelVersion` = `.name`
  (`laya@55cf4c4ebb4e` or `deterministic-v1`).
* Rank candidates with `score_batch`, not N × `score`.
* **Options must be unique.** Pass qualified ids (e.g. `"server/tool"`),
  not bare tool names: two servers often both expose `search`. A duplicate or
  empty option list is a caller `ValueError` (deliberately NOT an
  `InferenceError`), because a fallback model cannot fix a malformed question.
* Laya caveats from the model card that routing must design around:
  - The base checkpoint is **near chance zero-shot** on the vendor's
    typed-decisions benchmark (0.362). Domain fine-tuning is how it reaches
    0.766. Expect `route_confidence_floor` and the eval set to be the
    real tuning surface.
  - Choice accuracy degrades past ~20 options (`head_max_len` token budget).
    FR-04's hierarchical domain → operation → server → tool keeps each
    question small. Don't pass a 50-tool list to one `choice`.
  - The shipped checkpoint has an out-of-range temperature for **choice
    with 11+ options** (laya warns and clamps it to 0.5), so those
    probabilities are uncalibrated.
  - Ships over-confident until temperatures are refitted on our data
    (`laya.fit_temperatures`). This is future work, not in v0.1.
* `noul()` on Laya runs as a neutral two-option choice (`{"A":"yes","B":"no"}`)
  by default. The model card documents that native `noul` follows its labels on
  the English checkpoint (laya#156), and our CPU probe saw native `noul` drop to
  exactly 0.0 on negatives. `noul_mode="native"` is kept for A/B on real data.

## 6. Contract for the catalog/retrieval workstreams

* After discovery sync: `embed_pending_tools(session, engine.embedding_backend())`,
  then the caller commits. It re-embeds a tool when its vector is NULL, when
  sha256(`"name: description [tags]"`) changed (tags sorted and de-duplicated),
  or when the backend name differs. Unchanged tools never reach the model.
  It does not bump `updated_at`. Writes are optimistic
  (`WHERE id AND updated_at = value-read`): a tool that a concurrent sync
  changed is skipped (`EmbedReport.conflicts`) and re-embedded on the next
  run. This relies on every metadata write going through the ORM, whose
  `onupdate` bumps `updated_at`. A raw SQL writer that leaves `updated_at`
  alone would defeat it.
* `EmbeddingRuntimeError` (BGE encode failure, e.g. CUDA OOM) propagates out
  of `embed_pending_tools`. The caller rolls back and retries on the next refresh.
* Retrieval MUST filter `embedding_backend == engine.embedding_backend().name`
  before any vector comparison (scoping #5).
* A handle from `embedding_backend()` raises `InferenceError` if the engine
  has since reloaded onto a different backend. Re-acquire it; this protects
  provenance.
* BGE v1.5 is used without the optional query instruction prefix
  (`"Represent this sentence for searching relevant passages: "`). Retrieval
  may want to A/B that on the eval set.

## 7. Empirical facts established on this box (2026-10-08)

* Installed: torch 2.14.1+cpu, transformers 5.19.0, sentence-transformers
  6.1.0, laya 0.4.0, Python 3.12. 112 CPU threads visible (torch uses 56).
  This is a server, not a laptop CPU, so CPU numbers here are an optimistic
  baseline.
* Laya weights: `convaiinnovations/laya` at the reviewed SHA
  `55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851` (from
  `laya.revisions.PINNED_REVISIONS`). Hub HEAD was `7b928d8…`; the card
  says the checkpoints are unchanged between them. 421.3 M params, FP32 on
  CPU, ~808 MB download.
* BGE: `BAAI/bge-small-en-v1.5` at `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`,
  384-dim confirmed (declared and forward).
* The laya package never executes repo Python: it downloads only
  `rl_agent_config.json`, `model.safetensors`, `tokenizer/*`, `encoder/*` and
  builds the model from its own code.
* Gotcha: an offline `snapshot_download` must pass the SAME `allow_patterns`,
  or a partial snapshot is reported as incomplete (`IncompleteSnapshotError`).
  The adapter always passes them.
* Importing `laya` does `os.environ.setdefault("USE_TF", "0")`. It's a
  process-global side effect, but only when the Laya backend loads.
* Laya autocasts fp16/bf16 on CUDA by itself. BGE is `.half()`-ed by us on
  CUDA only. **Neither CUDA path is verified** (no GPU here); see
  `docs/hardware-validation.md`.

## 8. Latency-budget flag (for routing + validation day)

CPU baseline numbers are in `docs/hardware-validation.md`. Even on the
vendor's own T4 figures, Laya choice + `score_batch(20)` + noul exceeds the
150 ms warm p95 target. Unless the 4060 is much faster than a T4 on this
model, the routing design should plan to Laya-score only the top ~5
candidates by embedding (or use Laya only for choice/noul). Settle it with
data on validation day. The bench harness measures exactly that path
(`routeCompositeMs`).
