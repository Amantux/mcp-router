# Wave 4 — Skill Sources: integration notes

Branch `wave4/sources`. Everything here stays inside the skills fence. The items
below need a change OUTSIDE the fence, made by the integrator.

## 1. Router wiring (`src/mcprouter/api/app.py`)

The routers exist but `create_app` does not mount them yet. Add these next to
`app.include_router(servers_router)`:

```python
from mcprouter.api.routes_skill_sources import router as skill_sources_router
from mcprouter.api.routes_skills import router as skills_router

app.include_router(skill_sources_router)  # /api/v1/skill-sources (admin)
app.include_router(skills_router)         # /api/v1/skills (admin)
```

Both routers declare `dependencies=[Depends(require_admin)]` at the router level,
so every route requires admin. `tests/test_skills_api.py` mounts them on a bare app.

## 2. conftest table list

`tests/conftest.py` already lists `skills`, `skill_versions` and `skill_sources`
in its cleanup table list (scaffold commit 807fcb8). Nothing else is needed. If the
table list is ever reordered, keep `skill_versions` before `skills` before
`skill_sources`, because of the foreign keys.

## 3. Sync scheduling (open item)

`skill_sources.sync_interval_s` (default 3600, minimum 60) is stored and returned
by the API, but **nothing reads it**. Sources sync only through
`POST /api/v1/skill-sources/{id}/sync`. Proposal: add a skill-source tick to
`discovery/loop.py::SyncLoop`. The tick picks sources where
`enabled AND (last_synced_at IS NULL OR now - last_synced_at >= sync_interval_s)`
and calls the same service the sync route calls, one source at a time. It should
reuse SyncLoop's existing jitter and backoff. Do not start a second loop.

## 4. Owner decisions applied

| Decision | Behaviour | Where |
|---|---|---|
| Unknown frontmatter keys | Accepted, not rejected. Ingest adds the flag `unknown_frontmatter_keys`. The key names are stored sorted and comma-joined (capped at 1024 chars) in `skill_metadata["_unknown_keys"]`. Storing them as a string keeps `skill_metadata` a str→str map. A user metadata key with the same name is overwritten. Spec-required fields (`name`, `description`, the directory-name match, and the types and lengths) are still enforced strictly. | `skills/validate.py` |
| SKILL.md read cap | SKILL.md is opened with `O_NOFOLLOW` and checked with `fstat` (it must be a regular file). Ingest reads at most `skill_body_max_bytes + 16 KiB + 1` bytes. A larger file is skipped with the reason `skill_md_too_large` and is never read in full. | `skills/walker.py::_read_capped`, `ingest.py` |
| Nested skill dirs | A child directory that contains its own `SKILL.md` is ingested as a separate skill. Its files are left out of the parent's manifest, and its scripts do not count toward the parent's `has_scripts`. | `skills/walker.py::build_manifest` |

## 5. Ingest flags (`skills.ingest_flags`)

- `body_truncated`: the body is over `skill_body_max_bytes` and was cut on a UTF-8 boundary.
- `secret_like`: the body matches a secret pattern. The content is stored unchanged.
- `resource_oversize`: a manifest file is over `skill_resource_max_bytes`. Its `sha256` is null.
- `unknown_frontmatter_keys`: see the table in section 4.

## 6. Testbed

`python -m testbed.skills.generate --out DIR --skills N [--seed S] [--include-invalid] [--as-git]`

The generator writes deterministic skills across the five FR-04 domains:

- Near-duplicate pairs, such as `pdf-extract-text` and `pdf-text-extraction`, share a `duplicate_group`.
- `ground_truth.json` holds the expected skills. With `--include-invalid`, `invalid.json` maps each path to the expected skip reason.

The scale test (`tests/test_skills_scale.py`) ingests 1,000 skills in about 3.5 s
against the 60 s budget, so it runs by default.
