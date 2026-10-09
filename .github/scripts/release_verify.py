"""Release gate checks that need no network (D7). Run by release.yml `verify`.

    python .github/scripts/release_verify.py <tag>

Checks: the tag is `v` + the pyproject version, verbatim; the top `## ` heading
of CHANGELOG.md is that version. Prints `is_release=true|false` (a plain
`vX.Y.Z` tag, the only kind that may move `:latest`) for $GITHUB_OUTPUT.
The CI-green and ancestor-of-master checks need git/gh and live in the workflow.
"""

from __future__ import annotations

import re
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


def top_changelog_heading(root: Path = ROOT) -> str | None:
    for line in (root / "CHANGELOG.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            return line[3:].strip()
    return None


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
    return problems, bool(FINAL_RE.match(tag))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_verify.py <tag>", file=sys.stderr)
        return 2
    problems, is_release = check(argv[1])
    for p in problems:
        print(f"::error::{p}")
    if problems:
        return 1
    print(f"is_release={'true' if is_release else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
