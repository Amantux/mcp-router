# Spec — One-click policy presets (rule-template bundles with diff)

Shortlist #3. Value 4 / Cost 5 → value/cost 0.8.
Sourced from docs/backlog/proposals-2026-11-10.md (#5), refined per
docs/backlog/debate-2026-11-10.md (VERDICT: KEEP).

## Problem

Cold start is the coldest moment in the product. `docs/SPEC.md` /
`docs/scoping.md` make "policy rules are authored" the default flow,
and the setup wizard offers exactly one preset ("read-only starter") —
everywhere else a new operator faces a blank, unexplained rule model.
Verified in source:

- src/mcprouter/policy/rule_presets.py ALREADY ships four curated
  `PolicyRulePreset` templates (`read-only-agent`, `deny-execute`,
  `single-tool-no-approval`, `read-only-skill`), and
  `api/routes_policy.py` ALREADY exposes
  `GET /policy/rule-presets` and
  `POST /policy/rule-presets/{preset_id}/apply` (lines 528–535)
  via the single loader pair `list_presets()` / `get_preset()`.
- **Nothing in `ui/src` surfaces presets at all** — grep for
  "preset" hits only `setup/SetupPage.test.tsx`. So this is a
  UI-surfacing gap over existing backend, not a new subsystem
  (same shape of finding as #4's `stale_report()` case).

## Proposed behaviour (precise)

1. **Feed the existing loader, don't rebuild it.** Presets come from
   `list_presets()` only (CLAUDE.md: "one shared loader/helper per
   concern"). Extend `rule_presets.py` to 3–4 **bundles**, i.e. each
   preset's `template` becomes a list of `resourceKind`-based
   rule-dicts (`"tool"` / `"server"` / `"skill"`) that all deny by
   default; no new loader, helper, or fixture directory.
2. **Preset buttons live in two places, backed by one call:** the
   Policy page's empty rule-list state and the setup wizard (which
   today hardcodes only the read-only starter). Clicking a preset
   opens the current rule editor **pre-filled with an editable
   diff**: proposed rules highlighted against the (empty or existing)
   current set; the operator must explicitly save to persist — no
   hidden write on click.
3. **Every preset is a deny-by-default bundle; every gating question
   is decided here:** no preset may emit a rule that grants
   unapproved `execute` or unscoped `server` access. In particular
   `single-tool-no-approval` must be **reshaped to require
   approval** (or removed) before ship — its current template
   (`max_operation: "execute"`, `requires_approval: False`) is
   exactly the "over-permissive seed" the critique warns about.
   Bundle names: `read-only starter`, `dev sandbox`, `trusted +
   approvals` (per the KEEP note — the third keeps approvals
   gating everything risky).
4. **Guard, not vibes:** add tests/test_rule_presets.py asserting,
   per preset: every rule resolves through the policy engine to
   *deny* absent an explicit operator edit; and a mutation check —
   seed a bogus allow-through rule into a bundle and the named test
   fails (CLAUDE.md gate).

## Scope / non-goals

- **In scope:** presets as authored-rule templates, their
  Policy-page/wizard surfacing, the editable-diff preview, guard
  tests.
- **Non-goals / deliberately left out:** no "full trust" preset
  (a preset granting anything without approval = a button that
  widens access); no custom-bundle authoring or persistence (presets
  stay curated code); no auto-applying on wizard "Skip setup";
  not shipping rule *presets for presets* (a template library UI
  that hides hand-authoring) — hand-authored rule entry must stay
  exactly as reachable as before, since the docs celebrate that
  discipline.

## Data / migration impact

None. Presets are code-shipped fixtures; rules created from a preset
are ordinary `PolicyRule` rows via the existing apply route
(POST `/policy/rule-presets/{id}/apply`). No new tables, columns, or
`MCPR_*` keys — if implementation adds any new setting, the
`docs/reference/configuration.md` regeneration (`make docs-gen`) must
land in the same commit (CLAUDE.md).

## Test plan

Unit (CPU/zero-ML):
1. `list_presets()` returns ≥3 bundles; every rule in every bundle
   has `resourceKind` + `maxOperation` ≤ `read`, or
   `requiresApproval: True` — fails if any preset admits an
   allow-through.
2. Mutation check: tamper a fixture rule to `maxOperation:
   "execute", requiresApproval: False` → the named guard test
   fails; restore to green.
Functional:
3. Empty rule model → open each preset → diff renders → Save →
   rules appear in the list with the preset's scope labelled
   (e.g. "from: dev sandbox").
4. Existing hand-authored rules → preset apply shows a diff against
   them, doesn't clobber.

## Rollout / rollback

- One commit: `rule_presets.py` edits + Policy/wizard wiring +
  guard tests. Nothing to enable — no flag, no new settings, no
  data.
- Rollback = revert; authored-rule flow is unchanged either way,
  since this only pre-fills it.

## Acceptance criteria

- [ ] 3–4 curated bundles supplied ONLY through
      `list_presets()`/`get_preset()`; `single-tool-no-approval`
      reshaped so no bundle widens access.
- [ ] Policy page + wizard offer presets with an editable diff
      against current rules; saving is explicit and auditable
      (`createdBy: "preset:<id>"`).
- [ ] Guard tests 1–2 exist, run in `make check`, and actually
      break on mutation of any preset.
- [ ] Functional cases 3–4 pass; no new `MCPR_*` keys (or docs
      regenerated same-commit).

### POLISH (acceptance criteria too)

- [ ] **Empty state:** with no rules authored, the Policy page
      header reads `No rules yet — every tool, server and skill is
      denied.` and below it sit the preset buttons; never a blank
      editor, and never a disabled preset that copies into an empty
      model with no explanation.
- [ ] **Error state:** if `get_preset()` 404s (stale bookmark or
      bad id), the dialog stays closed with
      `That preset doesn't exist — check the list, or author
      rules normally.` and an "Author rule manually" route
      remains enabled; we never toast "Applied" for a preset that
      wasn't found.
- [ ] **Keyboard/focus:** `Tab` cycles the preset buttons in list
      order; opening a preset moves focus into the diff's
      save/cancel actions; Escape closes the dialog and **returns
      focus to the trigger button** (same contract as
      focus-and-rhythm's dialog rules — these cards must be
      implemented together to keep the copy/test sweep atomic).
- [ ] **Copy tone:** every preset card and every created-rule tag
      says `apply`, never "load"/"load preset"/"install", and the
      scope label keeps `approvals` (not "review" or
      "confirmation") — one word per concept, per the shared
      copy glossary.
- [ ] **Deliberate non-feature:** we don't seed an
      `allow-all-with-approvals` bundle — the "full trust" naming
      in the original proposal is cut rather than softened, since
      a preset that grants unapproved execution is the whole
      failure mode this spec exists to prevent.
