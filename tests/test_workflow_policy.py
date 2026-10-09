"""MT-7 workflow policy: release gating (D7), pinning and permissions (P-511).

Pure file checks; no network, no DB."""

from __future__ import annotations

import importlib.util
import re
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
    # Ancestor check: must exit non-zero when the SHA is not on master.
    anc = next(b for b in runs if "--is-ancestor" in b)
    assert re.search(r'--is-ancestor "\$GITHUB_SHA" origin/master \\\n\s*\|\| \{.*exit 1; \}', anc)
    # CI check: query pinned to this SHA, push event, master; success required.
    ci = next(b for b in runs if "workflows/ci.yml/runs" in b)
    query = re.search(r"workflows/ci\.yml/runs\?([^\"]+)\"", ci)
    assert query, ci
    params = dict(urllib.parse.parse_qsl(query.group(1)))
    assert params == {"head_sha": "${GITHUB_SHA}", "event": "push", "branch": "master"}
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
