"""SKILL.md parser + spec validation (table-driven)."""

from __future__ import annotations

import pytest

from mcprouter.skills.validate import SkillValidationError, parse_skill_md


def _md(fm: str, body: str = "Body\n") -> str:
    return f"---\n{fm}\n---\n{body}"


GOOD = "name: pdf-tools\ndescription: Work with PDFs."


@pytest.mark.parametrize(
    ("fm", "dir_name", "reason"),
    [
        ("name: PDF\ndescription: x", "PDF", "name must be"),
        ("name: -pdf\ndescription: x", "-pdf", "name must be"),
        ("name: pdf-\ndescription: x", "pdf-", "name must be"),
        ("name: pd--f\ndescription: x", "pd--f", "name must be"),
        ("name: " + "a" * 65 + "\ndescription: x", "a" * 65, "name must be"),
        ("name: ''\ndescription: x", "", "name must be"),
        ("name: pdf\ndescription: x", "other", "directory name"),
        ("name: pdf\ndescription: '   '", "pdf", "description"),
        ("name: pdf\ndescription: " + "d" * 1025, "pdf", "description"),
        ("name: pdf\ndescription: x\ncompatibility: " + "c" * 501, "pdf", "compatibility"),
        ("name: pdf\ndescription: x\nmetadata: {a: 1}", "pdf", "metadata"),
        ("name: pdf\ndescription: x\nmetadata: [a]", "pdf", "metadata"),
        ("name: pdf\ndescription: x\nbogus: 1", "pdf", "unsupported"),
        ("- a\n- b", "pdf", "mapping"),
        ("just a string", "pdf", "mapping"),
        ("name: pdf\nname: pdf\ndescription: x", "pdf", "duplicate"),
        ("name: !!python/object/apply:os.system ['id']\ndescription: x", "pdf", "valid YAML"),
        ("a: &x [1]\nname: pdf\ndescription: *x", "pdf", "aliases"),
        ("name: pdf\ndescription: [unterminated", "pdf", "valid YAML"),
        ("name: pdf\ndescription: x\n" + "#" * 17000, "pdf", "too large"),
        ("name: pdf\ndescription: x\nallowed-tools: [a]", "pdf", "allowed-tools"),
    ],
)
def test_rejections(fm: str, dir_name: str, reason: str) -> None:
    with pytest.raises(SkillValidationError, match=reason):
        parse_skill_md(_md(fm), dir_name=dir_name, body_max_bytes=1000)


@pytest.mark.parametrize("text", ["no frontmatter", "---\nname: x\n"])
def test_bad_framing(text: str) -> None:
    with pytest.raises(SkillValidationError):
        parse_skill_md(text, dir_name="x", body_max_bytes=1000)


def test_full_valid_with_unicode() -> None:
    fm = (
        "name: a1-b2\ndescription: Résumé ✓ 日本語\nlicense: MIT\ncompatibility: py3\n"
        'metadata: {author: zoë}\nallowed-tools: "Bash(git:*)  Read\\tWrite"'
    )
    p = parse_skill_md(_md(fm, "Héllo\n"), dir_name="a1-b2", body_max_bytes=1000)
    assert p.description == "Résumé ✓ 日本語"
    assert p.allowed_tools == ["Bash(git:*)", "Read", "Write"]
    assert p.metadata == {"author": "zoë"}
    assert p.body == "Héllo\n" and p.flags == []


def test_body_truncated_on_char_boundary() -> None:
    p = parse_skill_md(_md(GOOD, "é" * 10), dir_name="pdf-tools", body_max_bytes=5)
    assert p.body == "éé" and p.flags == ["body_truncated"]
    assert len(p.body.encode()) <= 5
