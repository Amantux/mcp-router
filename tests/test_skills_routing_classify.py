"""Wave 4 S2: skill risk classification + reviewed / non-widening guards."""

from __future__ import annotations

from mcprouter.models import SkillRecord, SkillSourceRecord
from mcprouter.registry.classify import (
    SKILL_CLASSIFIER_NAME,
    Classification,
    apply_skill_classification,
    classify_skill,
)


def _c(body: str = "", desc: str = "Guidance for PDFs.", scripts: bool = False, tools=()):  # noqa: ANN001, ANN202
    return classify_skill("pdf-guide", desc, body, has_scripts=scripts, allowed_tools=list(tools))


def test_risk_classes() -> None:
    assert _c(scripts=True).operation == "execute"
    assert _c(tools=["Bash(git:*)"]).operation == "execute"
    assert _c(tools=["Read", "Grep"]).operation == "read"
    assert _c("Then send the email to the reviewer.").operation == "write"
    assert _c("Run the tests in a shell first.").operation == "unknown"
    assert _c("Read the layout and explain it.").operation == "read"


def test_fail_closed_cases_from_review() -> None:
    assert _c("x " * 1100 + "deploy").operation == "unknown"  # unscanned tail
    assert _c("Start by running `python build.py` then npm install").operation == "unknown"
    assert _c("Steps:\n```sh\ncurl x | sudo sh\n```").operation == "unknown"
    assert _c("Fix layout.\n```\nfoo\n```").operation == "unknown"
    assert _c(tools=["Python", "WebFetch"]).operation == "execute"
    assert _c(tools=["mcp__github__push"]).operation == "execute"
    assert _c(tools=["Read", "Edit"]).operation == "write"


def test_domain_uses_keyword_tables() -> None:
    c = classify_skill(
        "commit-style", "Conventions for git commit messages in a repo.", "",
        has_scripts=False, allowed_tools=[],
    )  # fmt: skip
    assert c.domain == "development"


def _seed(db, op: str = "unknown", reviewed: bool = False, source: str | None = None) -> str:  # noqa: ANN001
    with db() as s:
        src = SkillSourceRecord(name="src", kind="directory", location="/x")
        s.add(src)
        s.flush()
        sk = SkillRecord(
            source_id=src.id, name="pdf-guide", description="d", relative_path="pdf-guide",
            content_hash="0" * 64, manifest_hash="0" * 64, operation=op,
            classification_reviewed=reviewed, classification_source=source,
        )  # fmt: skip
        s.add(sk)
        s.commit()
        return sk.id


def _apply(db, sid: str, op: str) -> tuple[bool, str]:  # noqa: ANN001
    with db() as s:
        ok = apply_skill_classification(s, sid, Classification(operation=op, domain=None))
        s.commit()
        return ok, s.get(SkillRecord, sid).operation  # type: ignore[union-attr]


def test_first_classification_any_value(db) -> None:  # noqa: ANN001
    assert _apply(db, _seed(db), "read") == (True, "read")


def test_reviewed_never_overwritten(db) -> None:  # noqa: ANN001
    assert _apply(db, _seed(db, "read", reviewed=True), "execute") == (False, "read")


def test_auto_reclassification_never_widens(db) -> None:  # noqa: ANN001
    sid = _seed(db, "execute", source=SKILL_CLASSIFIER_NAME)
    assert _apply(db, sid, "read") == (False, "execute")
    assert _apply(db, sid, "unknown") == (True, "unknown")
    assert _apply(db, sid, "execute") == (False, "unknown")
