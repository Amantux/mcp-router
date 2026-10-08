# Integration notes: registry / catalog / dedup track (`feat/registry`)

Scope: FR-02 catalog services, FR-05 dedup. No changes to `app.py`,
`models.py`, `interfaces.py`, `db.py`, `settings.py` or `conftest.py`.

## 1. Lines integration must add to `src/mcprouter/api/app.py`

```python
from mcprouter.api import routes_dedup, routes_tools
from mcprouter.registry.schema import init_registry

# in create_app(), right after init_db(engine):
init_registry(engine)
# with the other routers:
app.include_router(routes_tools.router)   # /api/v1/tools
app.include_router(routes_dedup.router)   # /api/v1/dedup
```

`init_registry(engine)` is idempotent (`CREATE ... IF NOT EXISTS`) and creates:

| index | table | purpose |
|---|---|---|
| `ix_tools_fts` | `mcp_tools` | GIN expression index for keyword search: name (weight A, camelCase split, `_ . - /` turned into spaces) + description (B) + tags (C, `json::text`) |
| `ux_dup_pair` | `duplicate_suggestions` | unique `(tool_a_id, tool_b_id)`, so concurrent dedup runs can't double-insert a pair (`ON CONFLICT DO NOTHING`) |

Without it, search still works but falls back to a sequential scan, and
`POST /api/v1/dedup/suggestions` **errors** because `ON CONFLICT` needs the
unique index. If Alembic owns DDL at integration, move both statements into
a migration verbatim. The search query and the index are built from the same
template (`registry/schema.py::tsv_sql`), and
`test_fts_query_uses_the_gin_expression_index` fails if they drift.

## 2. Admin auth (needed from the gateway/auth track)

There is no admin principal in the contract yet. Every catalog/dedup endpoint,
reads included, depends on `registry.api_deps.require_admin`:

* no `MCPR_AGENT_KEYS`: auth is disabled instance-wide (scoping §6) and the actor is `local-dev`;
* keys configured: **403, fail-closed**, until integration supplies a real
  admin authenticator via
  `app.dependency_overrides[require_admin] = <dep returning actor str>`
  (or by replacing the body). Mutation-checked: with the gate forced open,
  all 9 parametrised `test_management_api_fails_closed_*` cases fail.

## 3. Wire decisions

* **`categories` ↔ `capabilities` column.** SPEC §8 `MCPTool.categories[]`
  is stored in `MCPToolRecord.capabilities`. `capabilities` never appears on
  the wire, and PATCH accepts `categories` only (`capabilities` gets a 422 via
  `extra="forbid"`). `domain` (the FR-04 hierarchy level) is a separate scalar.
* Tool wire shape (`registry/wire.py::ToolOut`): SPEC §8 fields plus
  `serverName, domain, classificationReviewed, available,
  stats{callCount,errorCount,avgLatencyMs,successRate}, embeddingBackend,
  createdAt, updatedAt, rank` (`rank` is only present when `q` was given).
  Pages are `{items,total,limit,offset}`, with limit ≤ 200.
* `PATCH /tools/{id}/classification`: omitted fields stay unchanged. An
  **empty body approves** the current auto classification. Both set
  `classificationReviewed=true`. `domain: null` clears the domain. A `null` for
  operation/lists gets a 422 (the columns are NOT NULL). Lists are trimmed and
  deduped, with at most 50 items of up to 100 chars each.
* There is no endpoint yet to un-review a tool (hand it back to automatic
  classification). Add one if the UI needs it.

## 4. Behaviour other tracks rely on

* **Search**: `plainto_tsquery('english')` with `&` rewritten to `|`, so any
  term matches and `ts_rank_cd` puts all-term and name hits first. Tie-breaks
  are call_count desc, then name, then id. Without `q`, results are ordered by
  name, then id.
* **Reviewed guard**: `registry.catalog.apply_auto_classification()` /
  `auto_classify()` refuse `classification_reviewed=true` rows in the UPDATE's
  WHERE clause, so a classifier holding a stale read also loses to a human.
  **The inference track must write classifications through these functions,
  not with direct ORM assignment.** If you assign fields directly, you bypass
  the guard.
* **Rule classifier** (`registry/classify.py`, `ToolClassifier` protocol,
  `RuleBasedClassifier.name == "rules-v1"`) is conservative. It returns
  `unknown` / `None` rather than guess. ASSUMPTION: **the gateway treats
  operation `unknown` as most-restricted (≥ execute).** If the policy engine
  ever treats `unknown` as permissive, this classifier widens access. The
  policy track needs to confirm this.
  The auto writer only overwrites `capabilities` when the classifier supplies
  some. It always writes `domain` and `operation`, so a later rules run
  overwrites an earlier ML result. Precedence between classifiers is not
  modelled (that would need a provenance column; see §6).
* **Usage stats**: `registry.stats.record_execution(session, tool_id, ok=,
  latency_ms=)` is for the execution manager. It's one atomic UPDATE:
  call_count+1, error_count+1 on failure, and `avg_latency_ms` as an EMA with
  **alpha 0.2** (the first observation seeds it, and failures count toward
  latency). It doesn't bump `updated_at`. It returns False for an unknown
  tool, and raises `InvalidArgument` for a negative or non-finite latency.
  The caller commits.
* **Dedup** (`dedup/detect.py::run_dedup`):
  * Pairs come only from the same non-null domain, and both tools need an
    embedding from the **same** `embedding_backend`. Missing, mismatched and
    zero vectors are skipped.
  * Operations must match, or one of them must be `unknown`.
  * Score = 0.6·cosine + 0.2·name-token Jaccard + 0.2·input-property Jaccard.
    Two tools with no input properties count as 1.0. The default threshold is
    0.85, and the cosine pre-filter is exact.
  * Preferred tool: success rate (≥10 calls each, Δ≥5pt), then latency
    (≤80% of the other, ≥10 calls each), then a strictly narrower scope set.
    Otherwise none.
  * Re-runs refresh OPEN pairs. Accepted and dismissed pairs are never
    re-suggested, which goes further than the brief's "dedupe open".
  * An open pair that drops below the threshold stays open with its old
    score.
* **FR-05**: accept and dismiss only change the suggestion row. Acting on a
  suggestion is a separate `POST /tools/{id}/disable`. Mutation-checked in
  both directions: "disable the loser" and "re-enable" each fail a test.

## 5. interfaces.py

No additions. The classifier protocol lives in `registry/classify.py` as the
brief asked. Promote it to `interfaces.py` at integration if the inference
track implements it.

## 6. Contract needs (models.py, for integration to decide)

* `DuplicateSuggestion`: there are no resolution columns. The actor and the
  dismissal justification are currently **appended to `rationale`**
  (`"\nDismissed by <actor>: <note>"`) and audit-logged. Proposed columns:
  `resolved_by`, `resolved_at`, `resolution_note`.
* `MCPToolRecord`: a `classification_source` / `classified_by` column
  (`rules-v1`, `laya@…`, `human`) would let classifiers respect each other's
  precedence, and would make reviews auditable without logs.
* Classification overrides and enable/disable are recorded **only as audit
  log lines** (`mcprouter.audit` logger). No `ToolVersionRecord` row is
  written, so this track doesn't fight the discovery track's version
  counter. If "registry changes versioned/auditable" (SPEC §13) should
  cover these, add `change_kind="metadata"` rows at integration.

## 7. Audit log format

Logger `mcprouter.audit`, one INFO line per action, for example:

```
tool.disable actor='local-dev' tool_id='…' tool='list_issues' server_id='…' previous='True'
```

Events: `tool.enable`, `tool.disable`, `tool.classification.override`,
`dedup.run`, `dedup.accept`, `dedup.dismiss`. Each field is capped at 300
chars and `repr()`-quoted, so CR/LF and control characters come out escaped
and a value can't fake a field.
