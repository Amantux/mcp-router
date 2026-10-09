"""Release gate checks that need no network (D7). Run by release.yml `verify`.

    python .github/scripts/release_verify.py <tag>

Checks: the tag is `v` + the pyproject version, verbatim; the top versioned
`## ` heading of CHANGELOG.md is that version (Keep a Changelog: `## [X.Y.Z] -
date` or `## X.Y.Z`; a leading `## [Unreleased]` is skipped, so tagging without
moving its entries under the new version still fails). Prints `is_release=true|false` for
$GITHUB_OUTPUT: true only for a plain `vX.Y.Z` tag that is also the highest
plain tag in the repo (`git tag -l`), so `:latest` never moves backwards.
The CI-green and ancestor-of-master checks need git/gh and live in the workflow.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TAG_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+")
FINAL_RE = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")


def pyproject_version(root: Path = ROOT) -> str:
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version: str = data["project"]["version"]
    return version


_HEADING_RE = re.compile(r"^## \[?([^\]\s]+)\]?(?:\s+-\s+.*)?$")


def top_changelog_heading(root: Path = ROOT) -> str | None:
    for line in (root / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
        m = _HEADING_RE.match(line.strip())
        if m and m.group(1).lower() != "unreleased":
            return m.group(1)
    return None


def _semver(tag: str) -> tuple[int, int, int]:
    major, minor, patch = tag[1:].split(".")
    return int(major), int(minor), int(patch)


def is_latest(tag: str, all_tags: list[str]) -> bool:
    """A plain vX.Y.Z that is >= every other plain tag."""
    if not FINAL_RE.match(tag):
        return False
    plain = [t for t in all_tags if FINAL_RE.match(t)]
    return all(_semver(tag) >= _semver(t) for t in plain)


def repo_tags(root: Path = ROOT) -> list[str]:
    out = subprocess.run(
        ["git", "tag", "-l", "v*"], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    return out.split()


def check(tag: str, root: Path = ROOT) -> tuple[list[str], bool]:
    """Return (problems, is_release). Empty problems = the tag may ship."""
    problems: list[str] = []
    if not TAG_RE.match(tag):
        problems.append(f"tag {tag!r} does not look like vX.Y.Z[suffix]")
    version = pyproject_version(root)
    if tag != f"v{version}":
        problems.append(f"tag {tag!r} != 'v' + pyproject version {version!r}")
    heading = top_changelog_heading(root)
    if heading != version:
        problems.append(f"top CHANGELOG.md heading {heading!r} != pyproject version {version!r}")
    return problems, bool(FINAL_RE.match(tag))  # plain tag; newest-ness via is_latest


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_verify.py <tag>", file=sys.stderr)
        return 2
    problems, plain = check(argv[1])
    for p in problems:
        print(f"::error::{p}")
    if problems:
        return 1
    latest = plain and is_latest(argv[1], repo_tags())
    print(f"is_release={'true' if latest else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
