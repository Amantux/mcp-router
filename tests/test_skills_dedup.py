"""Wave-4 S2e: skill<->skill and cross-kind skill<->tool dedup."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.dedup.detect import run_dedup
from mcprouter.dedup.review import accept_suggestion, dismiss_suggestion, list_suggestions
from mcprouter.models import DuplicateSuggestion, MCPToolRecord, SkillRecord, SkillSourceRecord
from mcprouter.registry.schema import init_registry
from tests.support.dedup import V_A, V_A2, V_FAR
from tests.support.registry_fixtures import make_server, make_tool

from .conftest import requires_db


@pytest.fixture()
def factory(db: sessionmaker[Session]) -> sessionmaker[Session]:
    init_registry(db.kw["bind"])
    return db


def _skill(s: Session, src: SkillSourceRecord, name: str, emb: object, **kw: object) -> SkillRecord:
    sk = SkillRecord(
        source_id=src.id,
        name=name,
        description=f"{name} skill",
        body="",
        relative_path=name,
        content_hash="0" * 64,
        manifest_hash="0" * 64,
        domain=kw.pop("domain", "documents"),
        embedding=emb,
        embedding_backend="hash",
        allowed_tools=kw.pop("allowed_tools", ["Read"]),
        **kw,
    )
    s.add(sk)
    s.flush()
    return sk


def _seed(s: Session) -> tuple[SkillRecord, SkillRecord, SkillRecord, MCPToolRecord]:
    a_src = SkillSourceRecord(name="anthropic", kind="directory", location="/tmp/a")
    b_src = SkillSourceRecord(name="team", kind="directory", location="/tmp/b")
    s.add_all([a_src, b_src])
    s.flush()
    a = _skill(s, a_src, "pdf-fill", V_A)
    b = _skill(s, b_src, "pdf-fill", V_A2)
    far = _skill(s, b_src, "git-log", V_FAR)
    srv = make_server(s, "pdfsrv")
    t = make_tool(
        s,
        srv,
        "fill_pdf",
        "Fill a PDF",
        domain="documents",
        operation="write",
        embedding=V_A,
        embedding_backend="hash",
    )
    s.commit()
    return a, b, far, t


@requires_db
def test_skill_pairs_and_cross_kind_are_suggested(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        a, b, far, t = _seed(s)
        run_dedup(s)
        s.commit()
        rows = s.scalars(select(DuplicateSuggestion)).all()
        pairs = {(r.tool_a_id, r.tool_b_id): r for r in rows}
        ska, skb = sorted([f"skill:{a.id}", f"skill:{b.id}"])
        assert (ska, skb) in pairs
        assert not any(f"skill:{far.id}" in k for k in pairs)
        cross = [r for r in rows if r.rationale.startswith("cross-kind:")]
        assert {(r.tool_a_id, r.tool_b_id) for r in cross} == {
            (t.id, f"skill:{a.id}"),
            (t.id, f"skill:{b.id}"),
        }
        # rerun is idempotent
        before = len(rows)
        run_dedup(s)
        s.commit()
        assert len(s.scalars(select(DuplicateSuggestion)).all()) == before


@requires_db
def test_accept_dismiss_never_flip_enabled_on_either_kind(
    factory: sessionmaker[Session],
) -> None:
    with factory() as s:
        a, b, _far, t = _seed(s)
        run_dedup(s)
        s.commit()
        rows = s.scalars(select(DuplicateSuggestion)).all()
        accept_suggestion(s, rows[0].id, actor="admin")
        dismiss_suggestion(s, rows[1].id, actor="admin", justification="different")
        s.commit()
        for sk in (a, b):
            s.refresh(sk)
            assert sk.enabled is True
        s.refresh(t)
        assert t.enabled is True


@requires_db
def test_review_wire_has_kinds_and_skill_names(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        a, _b, _far, t = _seed(s)
        run_dedup(s)
        s.commit()
        items = list_suggestions(s).items
        cross = next(i for i in items if i.kind_a == "tool" and i.kind_b == "skill")
        assert cross.tool_a is not None and cross.tool_b is not None
        assert (cross.tool_a.server_name, cross.tool_a.name) == ("pdfsrv", "fill_pdf")
        assert cross.tool_b.name == "pdf-fill"
        assert cross.tool_b.server_name in {"anthropic", "team"}
        assert cross.tool_b.id.startswith("skill:")
        both = next(i for i in items if i.kind_a == "skill" and i.kind_b == "skill")
        assert both.tool_a is not None and both.tool_a.name == "pdf-fill"
        # usageEvidence tolerates skill ids (no crash, evidence present)
        assert both.usage_evidence is not None
