"""Wave 4 S2b: the retriever's skills leg (vector + keyword, one RRF with tools)."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from mcprouter.models import SkillRecord, SkillSourceRecord
from mcprouter.routing.retriever import HybridRetriever

from .conftest import requires_db
from .test_routing_fakes import FakeHashEmbedder, add_server, add_tool

pytestmark = requires_db

BOTH = ("tool", "skill")


def _skill(s: Session, src: SkillSourceRecord, name: str, desc: str, emb=None, **kw) -> str:  # noqa: ANN001
    sk = SkillRecord(
        source_id=src.id, name=name, description=desc, relative_path=name,
        content_hash="0" * 64, manifest_hash="0" * 64, operation="read",
        body_tokens_est=kw.pop("tokens", 120), **kw,
    )  # fmt: skip
    if emb is not None:
        sk.embedding = emb.embed([f"{name} {desc}"])[0]
        sk.embedding_backend = emb.name
    s.add(sk)
    s.flush()
    return sk.id


def _seed(db: sessionmaker[Session], emb: FakeHashEmbedder, **src_kw) -> dict[str, str]:  # noqa: ANN003
    with db() as s:
        gh = add_server(s, "github")
        add_tool(s, gh, "search_issues", "Search GitHub issues", embedder=emb)
        src = SkillSourceRecord(name="team-skills", kind="directory", location="/x", **src_kw)
        s.add(src)
        s.flush()
        ids = {
            "src": src.id,
            "pdf": _skill(s, src, "pdf-forms", "Fill PDF forms", emb, domain="documents"),
            # un-embedded: reachable by keyword only (body text, not description)
            "raw": _skill(s, src, "brand-voice", "House style", body="Use the zebra tone."),
            "off": _skill(s, src, "pdf-merge", "Merge PDF files", emb, enabled=False),
            "gone": _skill(s, src, "pdf-split", "Split PDF files", emb, available=False),
        }
        s.commit()
        return ids


def test_skill_candidate_shape_and_union(db) -> None:  # noqa: ANN001
    emb = FakeHashEmbedder()
    ids = _seed(db, emb)
    got = HybridRetriever(db, emb).retrieve("fill pdf forms issues", limit=10, kinds=BOTH)
    kinds = {c.kind for c in got}
    assert kinds == {"tool", "skill"}
    pdf = next(c for c in got if c.tool_id == ids["pdf"])
    assert (pdf.kind, pdf.server_id, pdf.server_name, pdf.tool_name) == (
        "skill", ids["src"], "team-skills", "pdf-forms",
    )  # fmt: skip
    assert (pdf.domain, pdf.operation, pdf.body_tokens_est) == ("documents", "read", 120)
    assert "vector" in pdf.matched_on and "keyword:pdf" in pdf.matched_on


def test_disabled_or_unavailable_skill_never_retrieved(db) -> None:  # noqa: ANN001
    emb = FakeHashEmbedder()
    ids = _seed(db, emb)
    got = {
        c.tool_id
        for c in HybridRetriever(db, emb).retrieve("merge split pdf", limit=20, kinds=BOTH)
    }
    assert ids["off"] not in got and ids["gone"] not in got and ids["pdf"] in got


def test_offline_or_disabled_source_hides_skills(db) -> None:  # noqa: ANN001
    emb = FakeHashEmbedder()
    _seed(db, emb, status="offline")
    got = HybridRetriever(db, emb).retrieve("fill pdf forms", limit=20, kinds=BOTH)
    assert not [c for c in got if c.kind == "skill"]


def test_disabled_source_hides_skills(db) -> None:  # noqa: ANN001
    emb = FakeHashEmbedder()
    _seed(db, emb, enabled=False)
    got = HybridRetriever(db, emb).retrieve("fill pdf forms", limit=20, kinds=BOTH)
    assert not [c for c in got if c.kind == "skill"]


def test_unembedded_skill_reachable_by_body_keyword(db) -> None:  # noqa: ANN001
    emb = FakeHashEmbedder()
    ids = _seed(db, emb)
    got = HybridRetriever(db, emb).retrieve("zebra", limit=10, kinds=BOTH)
    hit = next(c for c in got if c.tool_id == ids["raw"])
    assert hit.matched_on == ["keyword:zebra"]


def test_kinds_filter(db) -> None:  # noqa: ANN001
    emb = FakeHashEmbedder()
    _seed(db, emb)
    r = HybridRetriever(db, emb)
    assert {c.kind for c in r.retrieve("pdf issues", limit=10, kinds=("tool",))} == {"tool"}
    assert {c.kind for c in r.retrieve("pdf issues", limit=10, kinds=("skill",))} == {"skill"}


def test_default_kinds_is_tools_only_until_pipeline_budgets_skills(db) -> None:  # noqa: ANN001
    emb = FakeHashEmbedder()
    _seed(db, emb)
    assert {c.kind for c in HybridRetriever(db, emb).retrieve("pdf issues", limit=10)} == {"tool"}
