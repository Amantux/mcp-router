"""SKILL.md parsing + Agent Skills spec validation.

Frontmatter is parsed with a SafeLoader subclass that additionally refuses
YAML aliases (billion-laughs bombs), duplicate keys and oversized
frontmatter. Python-object tags (``!!python/...``) are refused by SafeLoader
itself. Bodies over ``skill_body_max_bytes`` are cut at the cap (on a UTF-8
character boundary) and flagged ``body_truncated``; the skill is still
cataloged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode, Node

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
FRONTMATTER_MAX_BYTES = 16 * 1024
_ALLOWED_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


class SkillValidationError(ValueError):
    """Curated, path-free reason a SKILL.md was rejected."""


class _StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent: Node | None, index: int) -> Node | None:
        if self.check_event(AliasEvent):
            raise SkillValidationError("frontmatter must not use YAML aliases")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise SkillValidationError("frontmatter has a duplicate key")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


@dataclass
class ParsedSkill:
    name: str
    description: str
    body: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def split_frontmatter(text: str) -> tuple[str, str]:
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != "---":
        raise SkillValidationError("SKILL.md must start with '---' frontmatter")
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r") == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :])
    raise SkillValidationError("frontmatter is not terminated by '---'")


def _load_yaml(fm: str) -> dict[str, Any]:
    if len(fm.encode("utf-8")) > FRONTMATTER_MAX_BYTES:
        raise SkillValidationError("frontmatter is too large")
    try:
        data = yaml.load(fm, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except SkillValidationError:
        raise
    except yaml.YAMLError as exc:
        raise SkillValidationError("frontmatter is not valid YAML") from exc
    if not isinstance(data, dict):
        raise SkillValidationError("frontmatter must be a mapping")
    return data


def _opt_str(data: dict[str, Any], key: str, max_len: int | None = None) -> str | None:
    v = data.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise SkillValidationError(f"{key} must be a string")
    if max_len is not None and len(v) > max_len:
        raise SkillValidationError(f"{key} exceeds {max_len} characters")
    return v


def _truncate_utf8(body: str, cap: int) -> tuple[str, bool]:
    raw = body.encode("utf-8")
    if len(raw) <= cap:
        return body, False
    return raw[:cap].decode("utf-8", errors="ignore"), True


def parse_skill_md(text: str, *, dir_name: str, body_max_bytes: int) -> ParsedSkill:
    fm, body = split_frontmatter(text)
    try:
        data = _load_yaml(fm)
    except (TypeError, RecursionError) as exc:  # unhashable keys, deep nesting
        raise SkillValidationError("frontmatter is not valid YAML") from exc
    # Owner decision (wave 4): unknown top-level keys are ACCEPTED, not rejected --
    # real-world skills carry vendor keys. Their names are recorded (sorted,
    # comma-joined) in metadata["_unknown_keys"] and flagged
    # `unknown_frontmatter_keys`. Spec-required fields stay strictly enforced.
    unknown = sorted(set(map(str, data)) - _ALLOWED_KEYS)
    name = data.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or not NAME_RE.fullmatch(name):
        raise SkillValidationError("name must be 1-64 chars of [a-z0-9] joined by single hyphens")
    if name != dir_name:
        raise SkillValidationError("name must equal the skill directory name")
    desc = data.get("description")
    if not isinstance(desc, str) or not desc.strip() or len(desc) > 1024:
        raise SkillValidationError("description must be a non-empty string of at most 1024 chars")
    meta = data.get("metadata") or {}
    if not isinstance(meta, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in meta.items()
    ):
        raise SkillValidationError("metadata must map strings to strings")
    tools_raw = _opt_str(data, "allowed-tools")
    body, truncated = _truncate_utf8(body, body_max_bytes)
    meta = dict(meta)
    flags = ["body_truncated"] if truncated else []
    if unknown:
        meta["_unknown_keys"] = ",".join(unknown)[:1024]
        flags.append("unknown_frontmatter_keys")
    return ParsedSkill(
        name=name,
        description=desc,
        body=body,
        license=_opt_str(data, "license"),
        compatibility=_opt_str(data, "compatibility", 500),
        metadata=meta,
        allowed_tools=tools_raw.split() if tools_raw else [],
        flags=flags,
    )
