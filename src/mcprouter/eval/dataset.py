"""Eval dataset format (JSONL, one case per line; blank lines ignored).

Fields:
  id               str, unique within the dataset                    (required)
  query            str, non-blank                                    (required)
  category         str, e.g. direct|overlapping|ambiguous|unavailable|
                   unauthorized|multistep|no_match                   (required)
  expected_tools   ["server/tool", ...] ranked by preference. Any of them at
                   rank 1 counts as a top-1 hit (ambiguous and multi-step
                   cases list every acceptable/needed tool).        (default [])
  forbidden_tools  ["server/tool", ...] that must NEVER be exposed — tools on
                   unavailable servers, or tools the agent is not authorized
                   for.                                              (default [])
  allowed_servers  [server name, ...] | null                         (default null)
  agent_id         str                                     (default "eval-agent")
  expect_no_match  bool — correct answer is no_match=true; requires empty
                   expected_tools.                                   (default false)
  expected_skills  ["source/name", ...] ranked by preference (Wave 4). Matched on
                   "<skill source>/<skill name>". A bare "name" is still accepted
                   but AMBIGUOUS: it matches that name from any source.
  forbidden_skills ["source/name", ...] that must NEVER be shown; any shown one
                   counts as an unauthorized skill exposure.
  kinds            subset of ["tool", "skill"] the case exercises; derived from
                   which expected_* lists are non-empty when omitted. A case
                   with both kinds is a "mixed" case.
  expect_denied    bool — request targets an operation the agent is not
                   authorized for; requires forbidden_tools.         (default false)

Datasets ship in `eval/datasets/<name>.jsonl`; `load_named` resolves a name
against the directory LISTING (a whitelist), never by joining caller input
into a path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATASETS_DIR = Path(__file__).parent / "datasets"
_KINDS = {"tool", "skill"}

_ALLOWED_KEYS = {
    "id",
    "query",
    "category",
    "expected_tools",
    "forbidden_tools",
    "expected_skills",
    "forbidden_skills",
    "kinds",
    "allowed_servers",
    "agent_id",
    "expect_no_match",
    "expect_denied",
}


class DatasetError(ValueError):
    """Curated, caller-safe dataset problem."""


@dataclass(frozen=True)
class EvalCase:
    id: str
    query: str
    category: str
    expected_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    allowed_servers: tuple[str, ...] | None = None
    agent_id: str = "eval-agent"
    expect_no_match: bool = False
    expect_denied: bool = False
    expected_skills: tuple[str, ...] = ()
    forbidden_skills: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ("tool",)


def available_datasets() -> dict[str, Path]:
    return {p.stem: p for p in sorted(DATASETS_DIR.glob("*.jsonl")) if p.is_file()}


def load_named(name: str) -> list[EvalCase]:
    path = available_datasets().get(name)
    if path is None:
        raise DatasetError("Unknown dataset name.")
    return parse_jsonl(path.read_text(encoding="utf-8"))


def parse_jsonl(content: str) -> list[EvalCase]:
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for lineno, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            raise DatasetError(f"line {lineno}: not valid JSON") from None
        case = _parse_case(raw, lineno)
        if case.id in seen:
            raise DatasetError(f"line {lineno}: duplicate case id")
        seen.add(case.id)
        cases.append(case)
    return cases


def _str(raw: dict[str, Any], key: str, lineno: int, default: str | None = None) -> str:
    v = raw.get(key, default)
    if not isinstance(v, str) or not v.strip():
        raise DatasetError(f"line {lineno}: '{key}' must be a non-empty string")
    return v.strip()


def _tool_list(raw: dict[str, Any], key: str, lineno: int) -> tuple[str, ...]:
    v = raw.get(key, [])
    if not isinstance(v, list) or not all(isinstance(t, str) for t in v):
        raise DatasetError(f"line {lineno}: '{key}' must be a list of strings")
    for t in v:
        server, _, tool = t.partition("/")
        if not server or not tool:
            raise DatasetError(f"line {lineno}: '{key}' entries must be 'server/tool'")
    return tuple(v)


def _parse_case(raw: object, lineno: int) -> EvalCase:
    if not isinstance(raw, dict):
        raise DatasetError(f"line {lineno}: each line must be a JSON object")
    extra = set(raw) - _ALLOWED_KEYS
    if extra:
        raise DatasetError(f"line {lineno}: unknown field(s) {sorted(extra)}")
    allowed = raw.get("allowed_servers")
    if allowed is not None and (
        not isinstance(allowed, list) or not all(isinstance(s, str) and s for s in allowed)
    ):
        raise DatasetError(f"line {lineno}: 'allowed_servers' must be null or a list of names")
    flags = {k: raw.get(k, False) for k in ("expect_no_match", "expect_denied")}
    if not all(isinstance(v, bool) for v in flags.values()):
        raise DatasetError(f"line {lineno}: expect_* flags must be booleans")
    skills = raw.get("expected_skills", [])
    if not isinstance(skills, list) or not all(isinstance(t, str) and t for t in skills):
        raise DatasetError(f"line {lineno}: 'expected_skills' must be a list of skill names")
    forbidden_skills = raw.get("forbidden_skills", [])
    if not isinstance(forbidden_skills, list) or not all(
        isinstance(t, str) and t for t in forbidden_skills
    ):
        raise DatasetError(f"line {lineno}: 'forbidden_skills' must be a list of skill refs")
    kinds_raw = raw.get("kinds")
    if kinds_raw is None:
        kinds = tuple(k for k, v in (("tool", raw.get("expected_tools")), ("skill", skills)) if v)
        kinds = kinds or ("tool",)
    elif not isinstance(kinds_raw, list) or not kinds_raw or not set(kinds_raw) <= _KINDS:
        raise DatasetError(f"line {lineno}: 'kinds' must be a non-empty subset of {sorted(_KINDS)}")
    else:
        kinds = tuple(k for k in ("tool", "skill") if k in kinds_raw)
    case = EvalCase(
        id=_str(raw, "id", lineno),
        query=_str(raw, "query", lineno),
        category=_str(raw, "category", lineno),
        expected_tools=_tool_list(raw, "expected_tools", lineno),
        forbidden_tools=_tool_list(raw, "forbidden_tools", lineno),
        allowed_servers=None if allowed is None else tuple(allowed),
        agent_id=_str(raw, "agent_id", lineno, "eval-agent"),
        expect_no_match=flags["expect_no_match"],
        expect_denied=flags["expect_denied"],
        expected_skills=tuple(skills),
        forbidden_skills=tuple(forbidden_skills),
        kinds=kinds,
    )
    if case.expect_no_match and (case.expected_tools or case.expected_skills):
        raise DatasetError(
            f"line {lineno}: expect_no_match cases cannot list expected_tools/skills"
        )
    if case.expect_denied and not case.forbidden_tools:
        raise DatasetError(f"line {lineno}: expect_denied cases must list forbidden_tools")
    return case
