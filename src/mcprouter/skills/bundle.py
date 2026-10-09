"""Bundle export (wave-4 S3): a zip of an agent's routed skills in spec layout
(`<name>/SKILL.md` + manifest files) for clients that only read
`~/.claude/skills`. All entry names are normalized relative paths (no zip-slip);
hard caps on skill count, per-file size and total size."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Sequence

from mcprouter.models import SkillRecord
from mcprouter.skills.serve import SkillFiles, SkillServeError, normalize_relpath

MAX_BUNDLE_SKILLS = 50
MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MARKER = ".mcp-router-bundle.json"


class BundleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def render_skill_md(skill: SkillRecord) -> str:
    """Reconstruct SKILL.md. Values are emitted as JSON strings, which are valid
    YAML double-quoted scalars, so author text can never break out of a key."""
    q = json.dumps
    lines = [f"name: {q(skill.name)}", f"description: {q(skill.description)}"]
    if skill.license:
        lines.append(f"license: {q(skill.license)}")
    if skill.compatibility:
        lines.append(f"compatibility: {q(skill.compatibility)}")
    if skill.skill_metadata:
        lines.append("metadata:")
        lines += [f"  {q(str(k))}: {q(str(v))}" for k, v in skill.skill_metadata.items()]
    if skill.allowed_tools:
        lines.append(f"allowed-tools: {q(' '.join(skill.allowed_tools))}")
    head = "\n".join(lines)
    return f"---\n{head}\n---\n{skill.body or ''}"


def build_bundle(
    items: Sequence[tuple[SkillRecord, SkillFiles]],
    *,
    max_skills: int = MAX_BUNDLE_SKILLS,
    max_total_bytes: int = MAX_BUNDLE_BYTES,
) -> tuple[bytes, list[str]]:
    """Return (zip bytes, skipped resource paths). Raises BundleError on caps."""
    if len(items) > max_skills:
        raise BundleError("too_many", f"Bundle exceeds {max_skills} skills.")
    buf = io.BytesIO()
    total = 0
    skipped: list[str] = []
    names: list[str] = []
    seen: set[str] = set()

    def add(zf: zipfile.ZipFile, arc: str, data: bytes) -> None:
        nonlocal total
        arc = normalize_relpath(arc)  # raises on '..'/absolute: zip-slip guard
        if arc in seen:
            return
        total += len(data)
        if total > max_total_bytes:
            raise BundleError("too_large", "Bundle exceeds the total size cap.")
        seen.add(arc)
        zf.writestr(arc, data)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for skill, files in items:
            try:
                top = normalize_relpath(skill.name)
            except SkillServeError:
                raise BundleError("invalid_name", "Skill name is not a safe path.") from None
            if "/" in top or top in names:
                raise BundleError("invalid_name", "Skill names must be unique single segments.")
            names.append(top)
            add(zf, f"{top}/SKILL.md", render_skill_md(skill).encode("utf-8"))
            for entry in skill.resource_manifest or []:
                path = str(entry.get("path", ""))
                try:
                    rc = files.read_resource(skill, path)
                except SkillServeError:
                    skipped.append(f"{top}/{path}")
                    continue
                data = rc.text.encode("utf-8") if rc.text is not None else (rc.blob or b"")
                add(zf, f"{top}/{rc.path}", data)
        add(zf, MARKER, json.dumps({"skills": names}, indent=2).encode("utf-8"))
    return buf.getvalue(), skipped
