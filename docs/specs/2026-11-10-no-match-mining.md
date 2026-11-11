# Spec — Mine `no_match` outcomes into "add a server" suggestions

Shortlist #2. Value 4 / Cost 3 → value/cost 1.33.
Sourced from `docs/backlog/proposals-2026-11-10.md` (#2), refined per
`docs/backlog/debate-2026-11-10.md` (VERDICT: KEEP).

## Problem

`no_match` is the honest terminal answer for a routing decision —
`routing/pipeline.py` sets `no_match = True` when the decision model
abstains or the `route_confidence_floor` (0.35) rejects everything, and
`routes_route.py` surfaces `no_match: bool` on the response — but nobody
aggregates the misses. An operator of a 100-server / 1,000-tool catalog
learns about coverage gaps only from support tickets. The funnel already
computes `surfaced → selected → succeeded` (`analytics/funnel.py`) and
`RouteFeedback` already exists (`models.py:398`, one row per
(decision, agent, target, source)) — `no_match` is the unlabeled sibling
waiting to be labelled.

## Proposed behaviour (precise)

1. **New pure-read module** `src/mcprouter/analytics/mining.py`,
   exporting `mine_no_match_intents(session, window: Window) ->
   list[GapSuggestion]`. Read-only over `routing_decisions` and
   `route_feedback`; no new tables, no writes, no execution surface, no
   authz change.
2. **Clustering is deterministic on the default backend.** Embed each
   distinct `no_match` query with the project's existing hash-embedding
   helper (same one `routing/` uses); greedy first-fit clusters in query
   order, cosine ≥ 0.82 → same cluster. With `[inference]` absent (the
   shipped default) this must be reproducible in CI and on the laptop
   alike — never call an LLM to cluster.
3. **Gate at a count threshold, decided here: ≥ 3 distinct `no_match`
   decisions in the window before any suggestion appears.** Below that
   the card renders "Not enough signal yet" — this keeps it out of the
   "you asked for the same thing three times and it's noise" toy zone
   flagged in the critique.
4. **Suggestion semantics.** Each cluster yields
   `{intentLabel: str, querySamples: [≤3 verbatim queries],
   suggestedAction: "register a server for this"}`; `intentLabel` is the
   most frequent noun-phrase-free two-word stem of member queries
   (deterministic: longest common word run). If a cluster's queries
   already match registered tools, it's dropped — suggest only true gaps.
5. **One surface, not a sprawl.** Render inside the existing funnel card
   in `ui/src/pages/AnalyticsPage.tsx` (it already imports
   `getAnalyticsSuggestions` / shows `WastedTool`s); do NOT add a second
   "underused" card or page — the critique's overlap point is
   binding: `no_match` mining must not become another "shown but unused"
   panel beside `wasted-exposure`.
6. **Copy.** Use the vocabulary of `docs/copy-glossary.md` as written by
   the focus-and-rhythm (#9, absorbing #8) spec — this card must say
   "surfaced" not "shown", "No match" not "Nothing found", etc.; the
   single-word-per-concept rule is a hard acceptance criterion, not
   decoration.

## Scope / non-goals

- **In scope:** read + cluster recorded `no_match` decisions; one card;
  deterministic hashing.
- **Non-goals / deliberately left out:** no auto-registration of
  servers, no button that creates anything, no "reply to this gap"
  inbox; **not** including "selected-but-failed" tools in mining (only
  true `no_match`, else every flaky server's tools drown the gap
  signal); no hand-tunable similarity knob in v1 (fix cosine at 0.82
  and the cluster-count gate at 3 — a third dial would be
  wish-list bait per the #6-style critique); no replay against
  historical candidate sets (route-replay was KILLED — decisions
  only persist final selections).
- **Do not** add any new `MCPR_*` setting — per the downstream note:
  "NO settings change; reuse existing machinery".

## Data / migration impact

None. Pure SELECT path over `routing_decisions` / `route_feedback`;
nothing persisted, nothing backfilled. Re-running over the same window
recomputes the same clusters (hash embeddings + first-fit = stable).

## Test plan

Unit (CPU/zero-ML):
1. Three near-identical `no_match` queries → exactly one cluster with
   `intentLabel` from the common stem; two queries → card hidden
   ("Not enough signal yet").
2. Cluster of queries that all route to registered tools → dropped.
3. Same window re-mined → byte-identical result (no LLM, no RNG).
Functional (`testbed.seed`, 15-decision window):
4. Seed `no_match` traffic; funnel card shows one suggestion with ≤3
   verbatim sample queries and "register a server for this".
5. Same run with the 3-decision gate → "Not enough signal yet".

## Rollout / rollback

- One commit: `analytics/mining.py` + funnel-card wiring + its tests.
  Ships dark-ish: with no `no_match` traffic it's just an empty state.
- Rollback = revert the commit; no schema, settings, or docs-gen churn
  (no new `MCPR_*` key, so `docs/reference/configuration.md` untouched).

## Acceptance criteria

- [ ] `mine_no_match_intents` reads only `routing_decisions` /
      `route_feedback`; clusters deterministically; gate = 3 distinct
      misses; already-routed intents dropped.
- [ ] Suggestion renders in the funnel card only; copy follows
      `docs/copy-glossary.md`.
- [ ] No new `MCPR_*` setting; no writes; no creation buttons.
- [ ] Unit cases 1–3 and functional cases 4–5 exist and pass under
      `make check`.

### POLISH (acceptance criteria too)

- [ ] **Empty state:** with < 3 `no_match` decisions in the window the
      funnel card's slot reads `Not enough signal yet — suggestions
      appear once a recurring gap shows up.`; never a spinner
      pinned at "0 clusters" or a blank grid.
- [ ] **Error state:** if the hash-embedding helper raises, the slot
      reads `Gap mining is unavailable right now — the funnel numbers
      are still accurate.` and the funnel itself still renders; we
      never show `Suggestions: 0` as if it were a real count.
- [ ] **Keyboard/focus:** the suggestion list items are buttons that
      expand inline (Enter/Space toggles, focus stays on the trigger
      when collapsed); no focus trap, no new dialog; `Tab` order:
      funnel table → slot → next card.
- [ ] **Copy tone:** `No match`, `surfaced`, `Estimated`
      — matching the rest of the product (see copy glossary);
      `intentLabel` and samples never invented — verbatim or
      stem-extracted from real queries only.
- [ ] **Deliberate non-feature:** tool-availability regressions
      (registered tool deleted mid-window) are NOT mined — only true
      `no_match`; and there is no per-cluster confidence, cost, or
      similarity tuning UI in v1.
