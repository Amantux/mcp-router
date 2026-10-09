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

## NOT done (budget) — next executor
- Playground Skills tab (activate as identity, body+resources+recordId, denied/pending renderer).
- Lens two-section (Tools/Skills) budgets + maxSkills slider; filtered-out table both kinds.
- Analytics kind chip, wasted-exposure skills, economy note; Duplicates cross-kind badges.
- Drawer Classification tab (ClassificationEditor is typed to MCPTool — needs a kind-generic prop or
  a `PATCH /skills/{id}/classification` save fn passed in) and Funnel tab.
- Lens/route types (`skills[]`, `maxSkillsApplied`) not added — S2 notes absent.
