# Haiku sweep: repo hygiene (MCP Router v0.6.0)

Read-only sweep. Nothing was deleted, pruned or pushed. Scope: leftovers, weirdness and
settings around the repo, not code.

**Verified (ran it):** git status, worktree list, branch merge state (against local `main` and
`origin/main`), dirty state of every worktree, Postgres database list and sizes, docker
image/container list, tracked-file scan, large-file scan, gh repo/protection/ruleset/PR/issue/run/release queries.

**Note on the checkout:** the task said branch `main`. The checkout at `/root/mcp-router` is on
`master` (73d3bc5, clean, tracks `origin/master`). Local `main` is at e944c88, 2 commits behind
`origin/main` (73d3bc5). `origin/main` and `origin/master` are the same commit. Version in
`pyproject.toml` is 0.6.0.

## 1. Git worktrees (35 entries = 1 main checkout + 34 linked)

All 34 linked branches are ancestors of `origin/main` (merged). "Dirty" is `git status --porcelain` line count.

| Path | Branch | Dirty | Recommended |
|---|---|---|---|
| ~/mcpr-wt-discovery | feat/discovery | 0 | delete (archive notes in docs/history already) |
| ~/mcpr-wt-gateway | feat/gateway | 0 | delete |
| ~/mcpr-wt-inference | feat/inference | 0 | delete |
| ~/mcpr-wt-registry | feat/registry | 0 | delete |
| ~/mcpr-wt-routing | feat/routing | 0 | delete |
| ~/mcpr-wt-ui | feat/ui | 0 | delete |
| ~/mcpr2-wt-analytics | wave2/analytics | 0 | delete |
| ~/mcpr2-wt-budgets | wave2/budgets | 0 | delete |
| ~/mcpr2-wt-ui | wave2/ui | 0 | delete |
| ~/mcpr3-wt-aoai | wave3/aoai | 0 | delete |
| ~/mcpr3-wt-backends | wave3/backends | 1 (`?? uv.lock`) | **triage**: confirm the untracked uv.lock is not wanted, then delete |
| ~/mcpr4-wt-s1 | wave4/sources | 4 (`M` skills/validate.py, skills/walker.py, 2 tests) | **keep until triaged**: uncommitted edits to tracked files; commit or discard deliberately |
| ~/mcpr4-wt-s2 | wave4/routing | 0 | delete |
| ~/mcpr4-wt-s2b | wave4/routing-pipeline | 0 | delete |
| ~/mcpr4-wt-s2c | wave4/routing-analytics | 0 | delete |
| ~/mcpr4-wt-s2f | wave4/skills-analytics | 0 | delete |
| ~/mcpr4-wt-s2g | wave4/skills-eval | 0 | delete |
| ~/mcpr4-wt-s3 | wave4/exposure | 0 | delete |
| ~/mcpr4-wt-s4 | wave4/ui | 0 | delete |
| ~/mcpr5-wt-container | wave5/container | 0 | delete |
| ~/mcpr5-wt-fba | wave5/metrics-analytics | 0 | delete |
| ~/mcpr5-wt-fbui | wave5/metrics-ui | 0 | delete |
| ~/mcpr5-wt-install | wave5/install | 0 | delete |
| ~/mcpr5-wt-metrics | wave5/metrics | 0 | delete |
| ~/mcpr5-wt-web | wave5/webserver | 0 | delete |
| ~/mcpr6-wt-e1 | wave6/e1 | 0 | delete |
| ~/mcpr6-wt-e2 | wave6/e2 | 0 | delete |
| ~/mcpr6-wt-e3 | wave6/e3 | 0 | delete |
| ~/mcpr6-wt-e4 | wave6/e4 | 1 (`?? docs/reference/`) | **triage**: untracked docs dir, check if it is generated; then delete |
| ~/mcpr6-wt-e5 | wave6/e5 | 0 | delete |
| ~/mcpr6-wt-e6 | wave6/e6 | 0 | delete |
| ~/mcpr6-wt-e7 | wave6/e7 | 0 | delete (its mcpr_e7_* DBs and mcpr-e7:* images go with it) |
| ~/mcpr6-wt-flake | wave6/ui-flake | 0 | delete (already merged into wave6/base) |
| ~/mcpr6-wt-w0 | wave6/base | 0 | delete |

Note: no `/root/ob*` worktrees exist. `git worktree list` shows no prunable entries.
Worktree removal does not touch `.git` objects, so the sweep is cheap to reverse as long as the branch is kept until the worktree is gone.

## 2. Local branches (`git branch -vv`)

| Branch | State | Recommended |
|---|---|---|
| master | checked out in /root/mcp-router, clean | keep |
| main | 2 behind origin/main, tracks origin/master (wrong upstream) | fast-forward or re-point to origin/main; see HS-H-001 |
| integrate/v0.1, integrate/wave2, integrate/wave4, integrate/wave5, integrate/wave6 | merged into origin/main | delete |
| wave4/scaffold | merged into origin/main | delete |
| 34 wave/feat branches | checked out in worktrees (section 1) | delete with their worktree |

## 3. Remote branches (`origin/*`)

- 10 `dependabot/*` branches are unmerged and each backs an open Dependabot PR (#2 to #11). Keep until the PR is merged or closed.
- About 25 other remote refs are merged into `origin/master`/`origin/main`. Re-verify with `git branch -r --merged origin/main` before deleting; the count may include the `origin/HEAD` alias. Recommended: delete the merged set in one batch.

## 4. Postgres databases (compose `mcp-router-db-1`)

| Database | Size | Looks like | Recommended |
|---|---|---|---|
| mcprouter | 16 MB | app DB | keep |
| mcprouter_test_template | 8.5 MB | test template (`_test_`) | keep if the test harness clones it; verify, else drop |
| postgres, template0, template1 | system | system | keep |
| mcpr_discovery | 9.7 MB | wave scratch | delete |
| mcpr_gateway | 22 MB | wave scratch | delete |
| mcpr_inference | 11 MB | wave scratch | delete |
| mcpr_registry | 8.5 MB | wave scratch | delete |
| mcpr_routing | 47 MB | wave scratch (largest) | delete |
| mcpr_w4s1 / mcpr_w4s2 / mcpr_w4s3 | 32 / 27 / 28 MB | wave4 scratch | delete (w4s1 worktree is dirty, so confirm first) |
| mcpr_mig_gen / mcpr_mig_try | 7.5 / 8.6 MB | migration scratch | delete |
| mcpr_e7_rss / mcpr_e7_test | 11 / 18 MB | wave6/e7 scratch | delete |

About 240 MB of scratch in total. All of it is recoverable from a migration replay.

## 5. Docker images and containers

- Containers: `docker ps -a | grep -i mcpr` returns nothing. No leftover containers.
- This repo's images:

| Image | Size | Recommended |
|---|---|---|
| mcp-router:local | 486 MB | keep (current dev image) |
| mcp-router:local-inference | 1.54 GB | keep |
| mcpr-e7:base, mcpr-e7:after, mcpr-e7:after2, mcpr-e7:before, mcpr-e7:uvx | 219 MB to 527 MB | delete (smoke leftovers; `base` and `after` share an ID) |
| mcpr-e7:inference | 1.78 GB | delete (keep mcp-router:local-inference) |
| mcpr-ui-probe:latest | 618 MB | delete |

- Not this repo, flagged only: alexproject-alex-mcp, job-tracker-playwright-mcp, sitelogix-fleet-fleet-mcp. Their names match "mcp" but are other projects. Leave them alone.

## 6. Tracked files

- `git ls-files` shows no `.log`, `.bak`, `.orig`, `.tmp`, `__pycache__`, `.pytest_cache` or `.DS_Store`. Clean.
- Largest tracked files: `uv.lock` 504 KB, `ui/package-lock.json` 228 KB, `docs/reference/openapi.json` 192 KB, `src/mcprouter/gateway/server.py` 56 KB. All legitimate; no large binaries.

## 7. docs/ layout

- `docs/audit/` holds 10 files, all committed 2026-10-09: A1 to A7 audit reports, `CI-GRIPES-haiku.md`, `CI-GRIPES-sonnet.md`, `PUNCHLIST.md` (55 KB). No README index.
- `docs/history/` already exists with 24 files (INTEGRATION_NOTES per wave, skills-plan.md, its own README.md).

## 8. GitHub repo settings (Amantux/mcp-router)

- Visibility PUBLIC; default branch `main`; issues on; wiki on (no content check). Description and 12 topics set.
- Branch protection on `main`: **none** (404). Rulesets: **none** (`[]`).
- Pages: not configured (404). Releases: none (`gh release list` empty), although `.github/workflows/release.yml` exists.
- Open issues: 0. Open PRs: 10, all Dependabot, all created 2026-10-09. PR #1 closed.
- README: no badges (zero shields/badge references).

## 9. Workflow runs (latest 6, by `--json`)

| Run | Workflow | Branch | Status |
|---|---|---|---|
| 37946421607 | CI | master | success |
| 37946421243 | Security scan | master | success |
| 37946420767 | Security scan | main | **in_progress** (created 14:44:18Z) |
| 37946420668 | CI | main | **in_progress** (created 14:44:18Z) |
| 37946237175 | CI | main | cancelled |
| 37946237172 | Security scan | main | success |

Two runs on `main` are still in progress at the time of the sweep. Check them by name (`gh run view 37946420668`) before relying on either workflow's result. Earlier CI FAIL run 37946236260 is older and superseded.

## Findings

| id | severity | evidence | finding | suggested action |
|---|---|---|---|---|
| HS-H-001 | medium | `git branch -vv`: main `[origin/master: behind 2]`; checkout on `master`; origin/main == origin/master == 73d3bc5 | Default branch is `main` on GitHub, but the local checkout is on `master`, and local `main` tracks `origin/master` and is stale. Two names for one trunk. | Check out `main` (fast-forward to origin/main), set upstream to origin/main, delete `origin/master` once nothing references it. |
| HS-H-002 | high | `branches/main/protection` 404; `rulesets` `[]` | `main` has no branch protection and no ruleset. Force-push and direct push are not blocked; CI is not required to pass. | Add a ruleset on `main`: require PR, require CI and Security scan checks, block force-push. Keep the release bot's push path in mind (see the release playbook). |
| HS-H-003 | medium | 34 linked worktrees, all branches merged into origin/main | Executor-fleet worktrees and their branches were never cleaned up. Harmless to history but clutters the host and holds disk. | Remove each worktree (`git worktree remove`), then delete its branch. Triage the three dirty ones first (HS-H-004). |
| HS-H-004 | medium | mcpr4-wt-s1: 4 modified tracked files; mcpr6-wt-e4: untracked docs/reference/; mcpr3-wt-backends: untracked uv.lock | Three worktrees hold uncommitted work. Deleting them blindly would lose it. | Review each diff. Commit to a branch or discard deliberately. Do not run `git worktree remove --force`. |
| HS-H-005 | medium | `pg_database`: 14 mcpr_* scratch DBs, about 240 MB | Executor scratch databases persist on the shared compose Postgres. | Drop mcpr_* scratch DBs after confirming no running process uses them. Keep `mcprouter` and check whether `mcprouter_test_template` is still cloned by tests. |
| HS-H-006 | low | `docker image ls`: 6 mcpr-e7:* and mcpr-ui-probe images, about 4 GB | Smoke images from wave 6 remain. `mcpr-e7:base` and `:after` share an image ID. | `docker image rm` the mcpr-e7 and mcpr-ui-probe tags. Keep mcp-router:local and :local-inference. |
| HS-H-007 | low | `git branch -vv`: integrate/v0.1, integrate/wave2/4/5/6, wave4/scaffold merged | Integration branches are merged and unused. | Delete locally. |
| HS-H-008 | low | `git branch -r`: about 25 remote refs merged into origin/master | Remote branches from merged waves were not deleted after merge. Each costs noise in listings. | Batch-delete after re-verifying the merged set with `git branch -r --merged origin/main`. Keep the 10 dependabot/* refs. |
| HS-H-009 | medium | `gh pr list`: 10 open Dependabot PRs, all opened 2026-10-09; #11 is the python 3.12 to 3.14 docker bump | A large first-day burst (grouped and separate majors such as typescript 7.0.2, react-router 8.4.0, jsdom 30.1.2). Majors are not yet reviewed. | Triage majors separately per the hygiene playbook; merge or close the minor/patch group. |
| HS-H-010 | medium | `gh run list`: 2 runs on `main` in_progress (created 14:44:18Z) | Two workflow runs on `main` are still in progress and their result is unknown. | Confirm they finish. Check each by name, not by `--limit 1`. |
| HS-H-011 | medium | `gh release list` empty; `release.yml` exists | Release workflow is present but no release has been published. v0.6.0 in pyproject has no matching tag or release. | Confirm whether `release.yml` has ever run to completion. Either cut v0.6.0 or document why it is not released yet. |
| HS-H-012 | low | `README.md`: zero badge references | README has no CI, security or version badges. | Add CI, Security scan and release badges (SHA-stable URLs), optional. |
| HS-H-013 | low | `docs/audit/`: 10 files, 2026-10-09; no README | Audit reports, PUNCHLIST and two CI-GRIPES lists sit at the top of docs/ with no index. They read as current but are historical. | Move to `docs/history/audit-2026-10/` or add a `docs/audit/README.md` that marks them as a dated snapshot. Keep PUNCHLIST open items in the issue tracker. |
| HS-H-014 | low | `docs/history/`: 24 files incl. README | History already exists, so the audit files are split across two places. | Consolidate under docs/history/ with one README index. |
| HS-H-015 | info | `git ls-files`, large-file scan | No committed logs, backups, caches or .DS_Store. Largest file is uv.lock at 504 KB. | None. Clean. |
| HS-H-016 | info | `git status` on checkout | Checkout is clean and no untracked files in the main checkout. | None. |
| HS-H-017 | info | `gh api pages`, `gh issue list` | Pages not configured, zero issues. Expected for this repo. | None unless a docs site is wanted. |
