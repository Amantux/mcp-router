"""Deterministic skill-corpus generator for the skill-sources testbed.

    python -m testbed.skills.generate --out DIR --skills N [--seed S]
        [--include-invalid] [--as-git]

Mirrors testbed/fleet.py: deterministic families across the five FR-04 domains,
a ground-truth sidecar, and near-duplicate pairs (``pdf-extract-text`` vs
``pdf-text-extraction``) that share a ``duplicate_group``.

Outputs:
- ``DIR/<name>/SKILL.md`` (+ ``scripts/``, ``references/``, ``assets/`` on some)
- ``DIR/ground_truth.json``: {name: {domain, risk_class, duplicate_group,
  has_scripts, expect_flags}} for every skill ingest should ACCEPT
- ``DIR/invalid.json`` (only with --include-invalid): {path: reason substring
  ingest should report in ``skipped``}
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

DOMAINS: dict[str, list[str]] = {
    "development": ["git", "docker", "python", "api", "ci", "lint"],
    "communication": ["email", "slack", "sms", "invite", "newsletter"],
    "files": ["pdf", "csv", "image", "zip", "docx"],
    "databases": ["postgres", "sqlite", "redis", "mongo", "schema"],
    "productivity": ["todo", "notes", "timer", "meeting", "report"],
}
# (verb, object, nominalisation, risk_class)
ACTIONS: list[tuple[str, str, str, str]] = [
    ("extract", "text", "extraction", "low"),
    ("search", "records", "search", "low"),
    ("summarize", "content", "summary", "low"),
    ("validate", "input", "validation", "low"),
    ("convert", "format", "conversion", "medium"),
    ("backup", "data", "backup", "medium"),
    ("send", "batch", "dispatch", "medium"),
    ("delete", "items", "deletion", "high"),
    ("deploy", "release", "deployment", "high"),
]
LICENSES = [None, "MIT", "Apache-2.0", "Proprietary. See LICENSE.txt"]
COMPAT = [None, "Requires python3", "Requires network access", "Designed for Claude Code"]
TOOLS = [None, "Read", "Read Bash(git:*)", "Read Write Bash(python3:*)"]
DUP_EVERY = 5  # every 5th family also gets a near-duplicate twin
Runner = Callable[[Sequence[str]], None]
Family = tuple[str, str, tuple[str, str, str, str], int]


def _families() -> list[Family]:
    """Deterministic (domain, topic, action, round) order; rounds add a -rN suffix."""
    base = [(d, t, a) for a in ACTIONS for d, topics in DOMAINS.items() for t in topics]
    return [(d, t, a, rnd) for rnd in range(64) for d, t, a in base]


def _md(name: str, desc: str, body: str, extra: dict[str, Any]) -> str:
    fm = {"name": name, "description": desc, **{k: v for k, v in extra.items() if v}}
    lines = ["---"] + [f"{k}: {json.dumps(v)}" for k, v in fm.items()] + ["---", body, ""]
    return "\n".join(lines)


def _write_skill(out: Path, name: str, desc: str, rng: random.Random, risk: str) -> bool:
    d = out / name
    d.mkdir(parents=True)
    meta = {"author": "testbed", "version": f"1.{rng.randrange(10)}"} if rng.random() < 0.5 else {}
    extra = {
        "license": rng.choice(LICENSES),
        "compatibility": rng.choice(COMPAT),
        "allowed-tools": rng.choice(TOOLS),
        "metadata": meta,
    }
    body = f"# {name}\n\nUse this skill to {desc.lower()}\n\nRisk: {risk}.\n"
    (d / "SKILL.md").write_text(_md(name, desc, body, extra))
    has_scripts = rng.random() < 0.3
    if has_scripts:
        (d / "scripts").mkdir()
        (d / "scripts" / "run.py").write_text(f"print({name!r})\n")
        (d / "scripts" / "run.sh").write_text(f"#!/bin/sh\necho {name}\n")
        (d / "references").mkdir()
        (d / "references" / "REFERENCE.md").write_text(f"# {name} reference\n")
    if rng.random() < 0.1:
        (d / "assets").mkdir()
        (d / "assets" / "template.txt").write_text("template\n")
    return has_scripts


def _write_invalid(out: Path) -> dict[str, str]:
    huge = "x" * (65536 + 16 * 1024 + 64)  # over default body cap + frontmatter cap
    cases = {
        "Bad_Name": ("Bad_Name", "body", "name must be", "Bad_Name"),
        "dir-mismatch": ("other-name", "body", "directory name", "dir-mismatch"),
        "oversize-body": ("oversize-body", huge, "skill_md_too_large", "oversize-body/SKILL.md"),
    }
    expected: dict[str, str] = {}
    for dirname, (name, body, reason, rel) in cases.items():
        (out / dirname).mkdir(parents=True)
        (out / dirname / "SKILL.md").write_text(_md(name, "Invalid fixture.", body, {}))
        expected[rel] = reason
    return expected


def generate(
    out: Path, n: int, *, seed: int = 0, include_invalid: bool = False
) -> dict[str, dict[str, Any]]:
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    truth: dict[str, dict[str, Any]] = {}
    for i, (domain, topic, (verb, obj, noun, risk), rnd) in enumerate(_families()):
        if len(truth) >= n:
            break
        sfx = f"-r{rnd}" if rnd else ""
        names = [f"{topic}-{verb}-{obj}{sfx}"]
        if i % DUP_EVERY == 0:
            names.append(f"{topic}-{obj}-{noun}{sfx}")
        group = f"{topic}-{noun}{sfx}" if len(names) > 1 else None
        for name in names[: n - len(truth)]:
            desc = f"{verb.capitalize()} {obj} for {topic} ({domain})."
            truth[name] = {
                "domain": domain,
                "risk_class": risk,
                "duplicate_group": group,
                "has_scripts": _write_skill(out, name, desc, rng, risk),
                "expect_flags": [],
            }
    if include_invalid:
        vk = out / "vendor-keys"
        vk.mkdir()
        (vk / "SKILL.md").write_text(_md("vendor-keys", "Has vendor keys.", "b", {"x-vendor": "1"}))
        truth["vendor-keys"] = {
            "domain": "development",
            "risk_class": "low",
            "duplicate_group": None,
            "has_scripts": False,
            "expect_flags": ["unknown_frontmatter_keys"],
        }
        (out / "invalid.json").write_text(json.dumps(_write_invalid(out), indent=1))
    (out / "ground_truth.json").write_text(json.dumps(truth, indent=1, sort_keys=True))
    return truth


def _default_runner(argv: Sequence[str]) -> None:
    ident = {"NAME": "testbed", "EMAIL": "testbed@example.invalid", "DATE": "2026-01-01T00:00Z"}
    env = dict(os.environ)
    for role in ("AUTHOR", "COMMITTER"):
        env.update({f"GIT_{role}_{k}": v for k, v in ident.items()})
    subprocess.run(list(argv), check=True, env=env, capture_output=True)  # noqa: S603


def make_git_repo(out: Path, runner: Runner = _default_runner) -> None:
    """git init + commit the generated tree (runner injectable, as in gitsource)."""
    g = ["git", "-C", str(out)]
    runner([*g, "init", "-q", "-b", "main"])
    runner([*g, "add", "-A"])
    runner([*g, "commit", "-q", "--no-gpg-sign", "-m", "testbed skills"])


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m testbed.skills.generate")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--skills", type=int, required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--include-invalid", action="store_true")
    ap.add_argument("--as-git", action="store_true", help="git init + commit the tree")
    a = ap.parse_args(argv)
    truth = generate(a.out, a.skills, seed=a.seed, include_invalid=a.include_invalid)
    if a.as_git:
        make_git_repo(a.out)
    print(f"wrote {len(truth)} skills to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
