# Wave 4 UI — integration notes (S4, branch wave4/ui)

## Shipped
- `/skill-sources` (SkillSourcesPage): list (name, kind badge, location = git host@ref or path tail,
  status, skill count, last synced + short commit), Add dialog (directory abs path OR https git URL + ref;
  https-only + no-credentials guard client-side, server must re-validate), per-row Sync → "Syncing…" →
  inline report (added/changed/removed + skipped table path/reason), enable/disable with a confirm naming
  the source.
- `/skills` (SkillsPage): search (200 ms debounce) + filters domain / risk class (operation) / source /
  enabled / available / reviewed / hasScripts, Clear filters, columns incl. body tokens (tabular) and
  ingest-flag chips; filtered-empty vs first-run-empty states; detail drawer tabs Overview (description,
  flags, license, compatibility, allowed-tools chips, metadata JSON), Body (plain `<pre>` text node —
  never Markdown/HTML), Resources (sorted by kind, sizes, oversize badge), Versions; "Try activation"
  link → `/playground?skill=<id>` (playground side NOT built yet).
- "Download bundle" (agent picker from `/principals`) → `GET /skills/bundle?agentId=` → blob download.
- Nav: "Skill sources", "Skills".

## CONTRACT guesses (ui/src/api/client.ts + types.ts, all marked `// CONTRACT:`)
| Call | Guess | Status |
|---|---|---|
| `GET/POST /api/v1/skill-sources` | list or `{items}`; POST `{name,kind,location,gitRef?}` | guessed (plan) |
| `PATCH /skill-sources/{id}` `{enabled}` | mirrors servers | **guessed — not in plan, confirm** |
| `POST /skill-sources/{id}/sync` | `{added,changed,removed,skipped[{path,reason}]}` | aligned with S1 WIP `skills/ingest.py` |
| `GET /skills` query | q, domain, operation, sourceId, enabled, available, reviewed, hasScripts, limit, offset | guessed; `hasScripts` filter not in plan list |
| `GET /skills/{id}` | flat: license, compatibility, metadata, allowedTools, resourceManifest[{path,size,sha256?,kind,oversize?}], versions[{version,contentHash,createdAt}] | partly aligned (S1 manifest entries `{path,size,kind}`); versions shape guessed |
| `GET /skills/{id}/body` | text body (non-JSON) | guessed |
| `GET /skills/bundle?agentId=` | zip | guessed |
| ingest flags | secret_like, body_truncated, oversize, resource_oversize (unknown → verbatim) | S1 WIP emits secret_like/oversize/resource_oversize |

Body + bundle use a new `requestRaw` (same bearer/credentials:"omit"/noteResponse/ApiError rules as
`request`, but no JSON parse); always presented as admin.

## Wave 4 S4b additions
- Playground: top-level Tools / Skills tabs (`?skill=<id>` or `?tab=skills` selects Skills). Skills tab: debounced
  search, Run-as rules identical to tools (agent key; admin-only must pick an agent → POST carries `agentId`),
  confirm dialog for `operation === "execute"` naming skill + identity, success shows audit record id, resources
  list, body as an inert `<pre>` text node. 403/404/429 → shared `ExecutionOutcomeView`
  (denied / unavailable / rate_limited). Page title is now "Playground" (nav label unchanged).
- Duplicates: `skill:<id>` refs load via `getSkill`; when a skill is in the pair, each side shows a "skill"/"tool"
  badge. Copy unchanged.

| Call | Guess | Status |
|---|---|---|
| `POST /skills/{id}/activate {agentId?}` → `{body, resources[{path,size,kind}], recordId}`; 403/404/429 | from S3 exposure notes | aligned with S3 *planned* shape, not verified against code |
| dedup `toolAId/toolBId = "skill:<id>"` | guessed | S2 routing notes absent |

## NOT done (budget) — next executor
- Lens two-section (Tools/Skills) budgets + maxSkills slider; filtered-out table kind badges.
- Analytics kind chip (All/Tools/Skills), skills in wasted exposure, economy caption.
- Skills drawer Classification tab (ClassificationEditor is typed to MCPTool — add a `save` prop) and Funnel tab.
- Route/simulate types (`skills[]`, `maxSkillsApplied`, maxSkills clamp): not added — S2 notes
  (/root/mcpr4-wt-s2/docs/INTEGRATION_NOTES-wave4-routing.md) still absent at S4b's run.

## S4c — final pass (skills in Lens + Analytics)

**Aligned (S2d item 2, read from /root/mcpr4-wt-s2b notes):** /route `skills[{source,skill,score,bodyTokensEst}]`
+ `max_skills_applied` (camelised); request `maxSkills` / `kinds`; /route/simulate `skills[{skillId,...}]`,
`maxSkillsApplied`, `kind` on candidatesConsidered / stages[].pruned / policyFiltered, `budgetClamps`
`{budget:"maxSkills",...}`. `RouteResponse.skills` is set only when the backend sends it (old shape unchanged).
Lens: Tools and Skills sections with score bars (skills show `~bodyTokensEst`), maxSkills slider
(0 = not requested), clamp card via the existing ClampView, kind badge column in Filtered out, per-kind
pruned counts on stage badges (derived from `stages[].pruned[].kind`).

**CONTRACT guesses (S2f notes say the analytics funnel/economy/profiles, `kind` filter and overview
`skills` are NOT done; names taken from the wave-4 brief):** `GET /analytics/tools?kind=tool|skill`
(omitted for All; client also filters rows by `kind`, treating a missing kind as "tool"), `kind` on
tool-funnel and wasted-exposure rows, `overview.skills {surfaced, activated, activationRate,
bodyTokensNotSent}` (Skills card + economy caption appear only when present).

**Not done (deferred):** item 4, the Skills drawer Classification + Funnel tabs. client.ts has no
skill-classification write (no `updateSkill`/PATCH /skills/{id}), so generalising ClassificationEditor
with a `save(fn)` prop would have no real consumer until that endpoint contract is confirmed.
