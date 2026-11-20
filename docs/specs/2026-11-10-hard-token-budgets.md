# Spec — Enforce (not just estimate) a per-agent token budget

Shortlist #1. Value 5 / Cost 2 → value/cost 2.5. **RECOMMENDED-NEXT.**
Sourced from docs/backlog/proposals-2026-11-10.md (#3), refined per
docs/backlog/debate-2026-11-10.md (VERDICT: KEEP).

## Problem

Today a "budget" only caps *how many* tools/skills are exposed — never how
many tokens those definitions *cost*. Verified in source:

- `interfaces.py::RouteRequest` carries `max_tools: int`,
  `max_servers: int | None`, `max_skills: int | None` — no token axis.
- `routing/pipeline.py` applies those caps *after ranking* via
  `_cap(ranked, max_tools, max_servers, max_skills)` (line 236) and treats
  `no_match` as an honest terminal answer when the confidence floor rejects
  everything — but nothing bounds the serialized schema bytes that survive
  the cap. A route to 8 fat-schema tools "saves" tokens in analytics while
  silently paying them on the wire.
- `analytics/tokens.py` already ships the deterministic estimator
  (`estimate_tokens` ≈ `ceil(chars/4)` over compact JSON of
  `{name, description, inputSchema}`; `ESTIMATOR` label
  `"chars/4 over compact JSON of name+description+inputSchema"`).
  `docs/analytics.md` documents these as estimates; enforcement of that
  estimate is what's missing, not a new engine.
- `docs/SPEC.md`'s headline objective is "minimize context-window
  consumption"; on the 8 GB RTX-4060 laptop target, bounded context IS the
  product. Today the claim is readable; this spec makes it enforced.

## Proposed behaviour (precise)

1. **One new axis on the existing envelope, not a parallel path.** Add
   `Settings.max_context_tokens: int | None = None` (`None` = no cap) to
   `interfaces.py`/`models.py` and register exactly one new knob
   `_s("max_context_tokens", "opt_int", "Estimated tokens of exposed tool
   definitions per route (empty: no cap).", lo=1)` → env
   `MCPR_MAX_CONTEXT_TOKENS` (`Settings.from_env`/`SETTINGS_SPEC`,
   settings.py:214+). Regenerate `docs/reference/configuration.md`
   (`make docs-gen`) **in the same commit** — CLAUDE.md gate.
2. **Enforcement point: after ranking, inside the same `_cap` machinery.**
   Candidates are already ranked by score desc (ties server/tool name);
   `route()` walks that order, summing `estimate_tokens` per supplied
   option's serialized definition, and truncates as soon as adding the next
   definition would exceed the budget. If even the cheapest single
   definition exceeds the budget → expose **zero** tools → honest
   `no_match` (`selected = [] if no_match` pattern, pipeline.py:236);
   never drop to 0 "to look cheaper" while still serving a look-alike.
   The clamp may only **narrow** the supplied option set — it must never
   widen access (deny by default; routing score never grants).
3. **CI-reproducible by construction.** Enforce on the `chars/4` proxy
   only — no tokenizer import. The `[inference]` extra stays optional;
   gates run CPU/zero-ML, so the estimator (and its documented ~±25%
   tolerance) must be what tests pin. Real-tokenizer checks are
   out of scope here — see Rollout.
4. **Wire contract changes (additions only).**
   - `RouteRequest` gains `max_context_tokens: int | None = None`.
   - Route + simulate responses gain
     `maxTokensApplied: int` and `estimator: "chars/4 over compact JSON
     of name+description+inputSchema"`; with the knob unset,
     `maxTokensApplied` = sum of estimates actually charged and
     `estimatedTokenSavings` stays labelled **estimated**
     (`SavingsBasis` is `null`/0 → `null`, never a measured 0).
   - Request `maxTokens`/`maxContextTokens` may **lower**, never raise,
     the principal's budget (mirrors routes_route.py:32 for
     maxTools/maxServers).
5. **UI/config surface.** Settings page shows the new knob beside the
   existing exposure budgets; Tools/Health pages label token figures as
   estimates per `docs/analytics.md` tone ("Estimated — ±25% by model").
   No new page, no new loader — the same `route/simulate` flow renders the
   extra line.

## Scope / non-goals

- **In scope:** enforcement of one token budget over exposed tool
  definitions, computed with the existing `estimate_tokens` proxy; its
  settings knob; wire + Settings-page surfacing.
- **Non-goals:** per-message / per-turn / per-session budgets; counting
  the `find_tools` meta-tool, approval suffixes, or MCP framing (mirrors
  `tokens.py` docstring — explicit exclusion); real-tokenizer (BPE)
  enforcement in CI — it lives only in `[inference]` and is
  non-reproducible on this dev box; editing cost assumptions (that's
  #6, merged into analytics settings); anything auto-generating tools.
- **Do NOT invent a second budget framework.** If someone proposes one,
  this spec is the veto: the token budget rides the existing
  tools/servers/skills envelope, applied post-ranking, narrowing only.

## Data / migration impact

None. No new tables, columns, or migrations: enforcement computes from the
live catalog at each route; historical `routing_decisions` rows are not
recomputed and don't need to be. `interfaces.py`/`models.py` gains are
additive fields with defaults, so existing contract signatures stay
source-compatible.

## Test plan

Unit (all CPU/zero-ML, `pytest`, `MCPR_REQUIRE_DB=1`):
1. `estimate_tokens` fixture pins — one fat + one thin schema — reproduce
   exactly: `ceil(len(rendered_definition(...)) / 4)`; `ESTIMATOR` label
   string asserted verbatim.
2. Budget equal to Σestimates of top-k → selects exactly those k; budget
   one token below the kth → selects k−1 (off-by-one boundary).
3. `max_context_tokens=None` → behaviour identical to today (regression).
4. Budget below the cheapest single option → empty selection,
   `no_match=true`; selection is always a prefix of the ranking.
5. A request or config value that would widen access (budget ignored, or
   budget applied before policy) makes the named test fail — mutation
   check per CLAUDE.md "security guards mutation-checked".
Functional (`testbed.seed` fleet):
6. `MCPR_MAX_CONTEXT_TOKENS` unset → 8 thin schemas exposed; set low →
   fewer; Settings page shows the knob; empty → `No cap` label.
7. Route against fat-schema tools with a mid-range budget → fewer tools
   than `maxTools` alone would expose; response labels
   `estimatedTokenSavings` as estimated.

## Rollout / rollback

- One commit: contract additions + enforcement in `routing/pipeline.py` +
  one `_s` knob in `settings.py` + Settings-page line + regenerated
  `docs/reference/configuration.md`. Deploy = pull; the knob is unset by
  default so shipped behaviour is unchanged until someone sets
  `MCPR_MAX_CONTEXT_TOKENS`.
- Real-tokenizer validation is a **follow-up on the target laptop**, not
  claimed done here; this repo enforces the deterministic proxy.
- Rollback = revert the one commit: knob, contract fields, and UI line
  disappear; nothing to unwind in the DB, and decisions recorded while it
  was deployed remain valid (they were computed against live catalogs).

## Acceptance criteria

- [ ] Enforcing clamps inside the existing post-ranking `_cap` machinery;
      never a second budget path; narrowing only.
- [ ] `chars/4` proxy enforced in CI; real-tokenizer path exercised only
      in `[inference]` / laptop validation, with `ESTIMATOR` label logged
      verbatim in responses.
- [ ] `MCPR_MAX_CONTEXT_TOKENS` registered in `SETTINGS_SPEC`;
      `docs/reference/configuration.md` regenerated in the same commit
      (`make docs-gen`).
- [ ] Unit cases 1–5 and functional cases 6–7 from the test plan exist
      and pass under `make check`.
- [ ] Guard for case 5 breaks when enforcement is mutated to widen or
      ignore the budget (CLAUDE.md gate).

### POLISH (acceptance criteria too)

- [ ] **Empty state:** with `MCPR_MAX_CONTEXT_TOKENS` unset, the
      Settings-page row reads `No cap` (bold `No cap` value, subtitle
      `Definitions may cost whatever the router selects`) — not an empty
      field, not `0`.
- [ ] **Error state:** an out-of-range or negative `MCPR_MAX_CONTEXT_TOKENS`
      value fails with `MCPR_MAX_CONTEXT_TOKENS must be a positive
      integer (got <redacted>): <hint>` naming the variable **without
      echoing the bad value** (CLAUDE.md: secrets/redaction rules, plus
      CR/LF scrubbed from any value-derived field).
- [ ] **Keyboard/focus:** the Settings-page budget input is reachable in
      DOM order; the "empty = no cap" and "cap = N estimated tokens"
      summaries are real `<p>`/`<label>` text, and the number formatting
      matches #9's rhythm rules (`p95`-style: `3 208 est. tokens`, not
      `3208est.tokens`); `Tab` traverses knob → save → reset, and
      `Enter` in the input triggers the same submit as the button.
- [ ] **Copy tone:** every surfaced figure says *est
      imated* / *estimatedTokenSavings*, never "actual" / "real";
      glossary words from docs/copy-glossary.md (per #8→#9:
      `surfaced` not *shown*, `Estimated` not *approx.*)
      — one word per concept, same as the rest of the product.
- [ ] **Deliberate non-feature:** no per-message, per-turn, or
      per-session budget aggregation, and no attempt to charge the
      `find_tools` meta-tool or MCP framing — those exclusions are
      stated in the relevant docstrings, not silently undefined.
