"""MT-7 workflow policy: release gating (D7), pinning and permissions (P-511).

Pure file checks; no network, no DB."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"


def load(name: str) -> dict[str, Any]:
    raw: dict[Any, Any] = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
    # PyYAML (YAML 1.1) reads the bare key `on` as boolean True.
    return {("on" if k is True else str(k)): v for k, v in raw.items()}


def _release_verify() -> Any:
    path = ROOT / ".github" / "scripts" / "release_verify.py"
    spec = importlib.util.spec_from_file_location("release_verify", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["release_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- release (D7)


def test_release_trigger_is_semver_tags_only() -> None:
    on = load("release.yml")["on"]
    assert set(on) == {"push"}, on
    assert on["push"] == {"tags": ["v[0-9]+.[0-9]+.[0-9]+*"]}


def test_release_images_need_verify() -> None:
    jobs = load("release.yml")["jobs"]
    assert "verify" in jobs
    for name in ("image", "image-inference"):
        needs = jobs[name].get("needs")
        needs = [needs] if isinstance(needs, str) else (needs or [])
        assert "verify" in needs, f"{name} must need verify"
        assert not jobs[name].get("continue-on-error"), f"{name} must fail visibly"


def test_release_verify_job_checks_everything() -> None:
    job = load("release.yml")["jobs"]["verify"]
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    assert "if" not in job and not job.get("continue-on-error")
    checkout = job["steps"][0]
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    runs = [s.get("run", "") for s in job["steps"]]
    for step in job["steps"]:
        assert not step.get("continue-on-error"), step
        assert "if" not in step, step
    for body in runs:
        assert "|| true" not in body and "true ||" not in body, body
    # Ancestor check: must exit non-zero when the SHA is not on the default branch.
    anc = next(b for b in runs if "--is-ancestor" in b)
    assert re.search(
        r'--is-ancestor "\$GITHUB_SHA" "origin/\$DEFAULT_BRANCH" \\\n\s*\|\| \{.*exit 1; \}', anc
    )
    # CI check: query pinned to this SHA, push event, the default branch; success required.
    ci = next(b for b in runs if "workflows/ci.yml/runs" in b)
    query = re.search(r"workflows/ci\.yml/runs\?([^\"]+)\"", ci)
    assert query, ci
    params = dict(urllib.parse.parse_qsl(query.group(1)))
    assert params == {"head_sha": "${GITHUB_SHA}", "event": "push", "branch": "${DEFAULT_BRANCH}"}
    assert '.c == "success"' in ci and 'if [ "$green" -ge 1 ]; then exit 0; fi' in ci
    assert ci.rstrip().endswith("exit 1")
    local = next(b for b in runs if "release_verify.py" in b)
    assert '|| { echo "$out"; exit 1; }' in local


def test_release_is_one_lane_and_builds_the_verified_sha() -> None:
    wf = load("release.yml")
    assert wf["concurrency"] == {"group": "release", "cancel-in-progress": False}
    for name in ("image", "image-inference"):
        checkout = wf["jobs"][name]["steps"][0]
        assert checkout["with"]["ref"] == "${{ github.sha }}"
    inf = next(
        s for s in wf["jobs"]["image-inference"]["steps"] if "build-push" in s.get("uses", "")
    )
    assert "@${{ needs.image.outputs.digest }}" in inf["with"]["build-args"]


def test_release_image_carries_the_verified_version_to_the_ui() -> None:
    jobs = load("release.yml")["jobs"]
    assert jobs["verify"]["outputs"]["version"] == "${{ steps.local.outputs.version }}"
    build = next(s for s in jobs["image"]["steps"] if "build-push-action" in s.get("uses", ""))
    assert "APP_VERSION=${{ needs.verify.outputs.version }}" in build["with"]["build-args"]


def test_release_images_have_provenance_and_sbom_and_gate_latest() -> None:
    jobs = load("release.yml")["jobs"]
    for name in ("image", "image-inference"):
        build = next(s for s in jobs[name]["steps"] if "build-push-action" in s.get("uses", ""))
        assert build["with"]["provenance"] == "mode=max"
        assert build["with"]["sbom"] is True
        tags = build["with"]["tags"]
        assert "latest" in tags and "needs.verify.outputs.is_release == 'true'" in tags


@pytest.mark.parametrize(
    ("tag", "ok", "is_release"),
    [
        ("v{v}", True, True),
        ("{v}", False, False),
        ("v9.9.9-rc.0", False, False),
        ("v{v}.1", False, False),
    ],
)
def test_release_verify_script(tag: str, ok: bool, is_release: bool) -> None:
    rv = _release_verify()
    version = rv.pyproject_version()
    problems, rel = rv.check(tag.format(v=version))
    assert (not problems) is ok, problems
    if ok:
        assert rel is is_release


def test_release_verify_rejects_changelog_drift(tmp_path: Path) -> None:
    rv = _release_verify()
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## 1.2.2\n")
    problems, _ = rv.check("v1.2.3", tmp_path)
    assert any("CHANGELOG" in p for p in problems)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## 1.2.3rc1\n\n## 1.2.3\n")
    problems, rel = rv.check("v1.2.3rc1", tmp_path)
    assert any("pyproject" in p for p in problems)
    # Keep a Changelog form; [Unreleased] is skipped, never counted as the release.
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n\n## [1.2.3] - 2026-10-09\n")
    assert rv.check("v1.2.3", tmp_path)[0] == []
    (tmp_path / "CHANGELOG.md").write_text("## [Unreleased]\n\n## [1.2.2] - 2026-10-01\n")
    assert any("CHANGELOG" in p for p in rv.check("v1.2.3", tmp_path)[0])


def test_release_verify_prerelease_never_latest(tmp_path: Path) -> None:
    rv = _release_verify()
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3rc1"\n')
    (tmp_path / "CHANGELOG.md").write_text("## 1.2.3rc1\n")
    problems, rel = rv.check("v1.2.3rc1", tmp_path)
    assert problems == [] and rel is False
    assert re.match(rv.TAG_RE, "v1.2.3rc1")


def test_latest_never_moves_backwards() -> None:
    rv = _release_verify()
    tags = ["v0.4.0", "v0.5.0", "v0.6.0-rc.1", "v0.6.0rc2"]
    assert rv.is_latest("v0.5.0", tags)
    assert rv.is_latest("v0.6.0", [*tags, "v0.6.0"])
    assert not rv.is_latest("v0.5.0", [*tags, "v0.6.0"])
    assert not rv.is_latest("v0.4.0", tags)
    assert not rv.is_latest("v0.6.0rc2", tags)
    assert rv.is_latest("v0.10.0", ["v0.9.0", "v0.10.0"])  # numeric, not lexical


# ---------------------------------------------------------------- policy (P-511)

USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)(.*)$")
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^\s+# v\d+\.\d+\.\d+\s*$")
WRITE_SCOPES = {"write"}


def workflows() -> list[str]:
    return sorted(p.name for p in WF.glob("*.yml"))


def test_expected_workflows_exist() -> None:
    assert {"ci.yml", "release.yml", "nightly.yml", "security-scan.yml"} <= set(workflows())


@pytest.mark.parametrize("name", workflows())
def test_every_action_is_sha_pinned_with_a_version_comment(name: str) -> None:
    bad: list[str] = []
    for line in (WF / name).read_text(encoding="utf-8").splitlines():
        m = USES.match(line)
        if not m or m.group(1).startswith("./"):
            continue
        if not PINNED.match(m.group(1)) or not VERSION_COMMENT.match(m.group(2)):
            bad.append(line.strip())
    assert bad == [], "pin to a commit SHA with a `# vX.Y.Z` comment"


@pytest.mark.parametrize("name", workflows())
def test_permissions_declared_and_writes_only_per_job(name: str) -> None:
    wf = load(name)
    assert "permissions" in wf, "declare top-level permissions"
    top = wf["permissions"]
    assert top == {} or set(top.values()) <= {"read", "none"}, f"workflow-level write: {top}"
    for job_name, job in wf["jobs"].items():
        perms = job.get("permissions", {})
        assert isinstance(perms, dict), job_name
        if "security-events" in perms:
            uploads = [
                s
                for s in job["steps"]
                if "codeql-action/analyze" in s.get("uses", "")
                or "codeql-action/upload-sarif" in s.get("uses", "")
            ]
            assert uploads, f"{job_name}: security-events: write without a SARIF upload"


def test_no_retries_anywhere() -> None:
    texts = [(WF / n).read_text() for n in workflows()]
    texts.append((ROOT / "pyproject.toml").read_text())
    for t in texts:
        assert not re.search(r"--reruns|rerun-failures|retry-on|max-attempts|flaky", t)


def test_ci_lanes_and_gate() -> None:
    ci = load("ci.yml")
    jobs = ci["jobs"]
    assert ci["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert set(jobs["ci-gate"]["needs"]) == set(jobs) - {"ci-gate"}
    assert jobs["ci-gate"]["if"] == "always()"
    runs = {n: "\n".join(s.get("run", "") for s in j.get("steps", [])) for n, j in jobs.items()}
    assert '-n auto -m "not e2e and not slow and not scale"' in runs["unit"]
    assert "--dist loadfile" in runs["e2e"] and '-m "e2e and not slow and not scale"' in runs["e2e"]
    assert "uv lock --check" in runs["lint-type"]
    assert "mypy_baseline.py" in runs["lint-type"]
    for lane in ("unit", "e2e"):
        assert jobs[lane]["env"]["MCPR_REQUIRE_DB"] == "1"
        uploads = [s for s in jobs[lane]["steps"] if "upload-artifact" in s.get("uses", "")]
        assert uploads and uploads[0]["if"] == "failure()"
    ui_setup = next(s for s in jobs["ui"]["steps"] if "setup-node" in s.get("uses", ""))
    assert ui_setup["with"]["node-version-file"] == ".nvmrc"
    assert (ROOT / ".nvmrc").read_text().strip() == "22"
    smoke_build = next(
        s for s in jobs["compose-smoke"]["steps"] if "build-push" in s.get("uses", "")
    )
    assert smoke_build["with"]["cache-from"].startswith("type=gha")


def _gate_script() -> str:
    step = load("ci.yml")["jobs"]["ci-gate"]["steps"][0]
    script: str = step["run"]
    return script


GATE_JOBS = ("LINT_TYPE", "UNIT", "E2E", "DOCS", "UI", "COMPOSE_SMOKE")


def _run_gate(**env: str) -> int:
    full = {"CHANGES": "success", **{k: "success" for k in GATE_JOBS}}
    full.update({"BACKEND": "true", "UI_REL": "true", "SMOKE_REL": "true", "DOCS_REL": "false"})
    full.update(env)
    proc = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", _gate_script()],
        env={"PATH": "/usr/bin:/bin", **full},
        capture_output=True,
        text=True,
    )
    return proc.returncode


def test_ci_gate_docs_only_pr_is_green_with_skips() -> None:
    skipped = {k: "skipped" for k in ("LINT_TYPE", "UNIT", "E2E", "UI", "COMPOSE_SMOKE")}
    assert (
        _run_gate(BACKEND="false", UI_REL="false", SMOKE_REL="false", DOCS_REL="true", **skipped)
        == 0
    )


@pytest.mark.parametrize("job", GATE_JOBS[:3])
def test_ci_gate_red_when_a_relevant_job_is_skipped_or_fails(job: str) -> None:
    assert _run_gate(**{job: "skipped"}) == 1
    assert _run_gate(**{job: "failure"}) == 1
    assert _run_gate(**{job: "cancelled"}) == 1


def test_ci_gate_red_when_changes_fails() -> None:
    assert _run_gate(CHANGES="failure") == 1


def test_dependabot_ecosystems() -> None:
    cfg = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    got = {(u["package-ecosystem"], u["directory"]) for u in cfg["updates"]}
    assert got == {("uv", "/"), ("npm", "/ui"), ("docker", "/"), ("github-actions", "/")}
    for u in cfg["updates"]:
        assert u["schedule"]["interval"] == "weekly"
    uv = next(u for u in cfg["updates"] if u["package-ecosystem"] == "uv")
    assert {"dependency-name": "mcp", "update-types": ["version-update:semver-major"]} in uv[
        "ignore"
    ]
    for u in cfg["updates"]:
        if u["package-ecosystem"] in {"uv", "npm"}:
            (group,) = u["groups"].values()
            assert group["update-types"] == ["minor", "patch"]  # majors arrive alone


def test_security_scan_jobs() -> None:
    sec = load("security-scan.yml")
    jobs = sec["jobs"]
    assert {
        "codeql",
        "python-deps",
        "npm-deps",
        "dependency-review",
        "trivy-fs",
        "trivy-image",
        "gitleaks",
        "sbom",
    } <= set(jobs)
    assert sec["permissions"] == {"contents": "read"}
    assert jobs["codeql"]["strategy"]["matrix"]["language"] == ["python", "javascript-typescript"]
    for advisory in ("python-deps", "npm-deps"):
        assert jobs[advisory]["continue-on-error"] is True
    review = next(
        s for s in jobs["dependency-review"]["steps"] if "dependency-review" in s.get("uses", "")
    )
    assert review["with"]["fail-on-severity"] == "high"
    gl = jobs["gitleaks"]["steps"]
    assert gl[0]["with"]["fetch-depth"] == 0
    assert gl[1]["env"]["GITLEAKS_CONFIG"] == ".gitleaks.toml"
    assert (ROOT / ".gitleaks.toml").is_file()


def test_nightly_runs_slow_and_scale() -> None:
    nightly = load("nightly.yml")
    assert "workflow_dispatch" in nightly["on"] and nightly["on"]["schedule"]
    job = nightly["jobs"]["slow-scale"]
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert '-m "slow or scale"' in runs and "[inference]" in runs
    assert job["env"]["MCPR_RUN_SLOW"] == "1" and job["env"]["MCPR_REQUIRE_DB"] == "1"
    assert any("actions/cache" in s.get("uses", "") for s in job["steps"])
    img = "\n".join(s.get("run", "") for s in nightly["jobs"]["inference-image"]["steps"])
    assert "Dockerfile.inference" in img and "scripts/smoke.sh" in img
