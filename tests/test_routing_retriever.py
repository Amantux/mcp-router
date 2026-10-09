"""Hybrid retriever (FR-03 candidate retrieval): pgvector cosine + Postgres
keyword search, fused with reciprocal-rank fusion."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from mcprouter.interfaces import Retriever
from mcprouter.routing.retriever import HybridRetriever
from tests.support.routing_fakes import FakeHashEmbedder, add_server, add_tool

from .conftest import requires_db

pytestmark = requires_db


def _seed(factory: sessionmaker[Session], emb: FakeHashEmbedder) -> dict[str, str]:
    ids: dict[str, str] = {}
    with factory() as s:
        gh = add_server(s, "github")
        slack = add_server(s, "slack")
        ids["search_issues"] = add_tool(
            s,
            gh,
            "search_issues",
            "Search GitHub issues by text",
            embedder=emb,
            domain="development",
            tags=["issues"],
        ).id
        ids["create_issue"] = add_tool(
            s,
            gh,
            "create_issue",
            "Open a new GitHub issue",
            embedder=emb,
            domain="development",
            operation="write",
        ).id
        ids["send_message"] = add_tool(
            s,
            slack,
            "send_message",
            "Post a message to a Slack channel",
            embedder=emb,
            domain="communication",
            operation="write",
        ).id
        # No embedding at all: discovered but not yet embedded.
        ids["transition_ticket"] = add_tool(
            s,
            gh,
            "transition_ticket",
            "Move a ticket through workflow states",
            embedder=None,
            domain="productivity",
            operation="write",
        ).id
        ids["gh"] = gh.id
        ids["slack"] = slack.id
        s.commit()
    return ids


def test_satisfies_protocol(db: sessionmaker[Session]) -> None:
    assert isinstance(HybridRetriever(db, FakeHashEmbedder()), Retriever)


def test_vector_and_keyword_legs_fuse(db: sessionmaker[Session]) -> None:
    emb = FakeHashEmbedder()
    ids = _seed(db, emb)
    got = HybridRetriever(db, emb).retrieve("search issues", limit=10)
    assert got[0].tool_id == ids["search_issues"]
    top = got[0]
    assert "vector" in top.matched_on
    assert "keyword:search" in top.matched_on and "keyword:issues" in top.matched_on
    assert top.server_name == "github" and top.domain == "development"
    assert top.operation == "read"
    # Fused scores are normalised and strictly ordered.
    assert 0.0 < got[-1].retrieval_score <= top.retrieval_score <= 1.0
    assert [c.retrieval_score for c in got] == sorted(
        (c.retrieval_score for c in got), reverse=True
    )


def test_tool_without_embedding_reachable_via_keyword_leg(db: sessionmaker[Session]) -> None:
    emb = FakeHashEmbedder()
    ids = _seed(db, emb)
    got = HybridRetriever(db, emb).retrieve("transition the ticket to done", limit=10)
    hit = [c for c in got if c.tool_id == ids["transition_ticket"]]
    assert hit, "un-embedded tool must still be retrievable by keyword"
    assert "vector" not in hit[0].matched_on
    assert any(m.startswith("keyword:") for m in hit[0].matched_on)


def test_vectors_from_other_backends_are_never_compared(db: sessionmaker[Session]) -> None:
    emb = FakeHashEmbedder()
    ids = _seed(db, emb)
    with db() as s:
        from mcprouter.models import MCPToolRecord

        t = s.get(MCPToolRecord, ids["send_message"])
        assert t is not None
        t.embedding_backend = "bge"  # same dims, different space
        s.commit()
    got = HybridRetriever(db, emb).retrieve("zzqx unrelated words", limit=10)
    assert ids["send_message"] not in {c.tool_id for c in got}


def test_filters_enabled_available_and_server_ids(db: sessionmaker[Session]) -> None:
    emb = FakeHashEmbedder()
    ids = _seed(db, emb)
    r = HybridRetriever(db, emb)
    only_slack = r.retrieve("issues message", limit=10, server_ids=[ids["slack"]])
    assert {c.server_name for c in only_slack} == {"slack"}
    assert r.retrieve("issues", limit=10, server_ids=[]) == []

    from mcprouter.models import MCPServerRecord, MCPToolRecord

    with db() as s:
        t = s.get(MCPToolRecord, ids["search_issues"])
        c = s.get(MCPToolRecord, ids["create_issue"])
        assert t is not None and c is not None
        t.enabled = False
        c.available = False
        s.commit()
    got = {c.tool_id for c in r.retrieve("search create issue", limit=10)}
    assert ids["search_issues"] not in got and ids["create_issue"] not in got
    # enabled_only=False is an explicit admin/simulation escape hatch.
    got_all = {c.tool_id for c in r.retrieve("search create issue", limit=10, enabled_only=False)}
    assert ids["search_issues"] in got_all

    with db() as s:
        srv = s.get(MCPServerRecord, ids["slack"])
        assert srv is not None
        srv.status = "offline"
        s.commit()
    assert all(c.server_name != "slack" for c in r.retrieve("send message", limit=10))


def test_limit_and_stopword_only_query(db: sessionmaker[Session]) -> None:
    emb = FakeHashEmbedder()
    _seed(db, emb)
    r = HybridRetriever(db, emb)
    assert len(r.retrieve("issue", limit=2)) == 2
    # Only stopwords: keyword leg is skipped, vector leg still answers.
    got = r.retrieve("the and of", limit=10)
    assert all(m == "vector" for c in got for m in c.matched_on)


def test_keyword_index_is_idempotent_and_matches_the_query(db: sessionmaker[Session]) -> None:
    """The GIN expression index must be built from the SAME expression the
    keyword leg queries, or Postgres silently stops using it."""
    from sqlalchemy import text

    from mcprouter.db import make_engine
    from mcprouter.routing.retriever import _KEYWORD_SQL, KEYWORD_INDEX, ensure_keyword_index
    from mcprouter.settings import Settings

    from .conftest import TEST_DB_URL

    eng = make_engine(Settings(database_url=TEST_DB_URL))
    ensure_keyword_index(eng)
    ensure_keyword_index(eng)
    _seed(db, FakeHashEmbedder())
    with db() as s:
        s.execute(text("SET LOCAL enable_seqscan = off"))
        s.execute(text("SET LOCAL enable_indexscan = off"))  # tiny table: force the choice
        plan = "\n".join(
            r[0]
            for r in s.execute(
                text("EXPLAIN " + str(_KEYWORD_SQL)),
                {"words": ["issues"], "enabled_only": True, "server_ids": None, "lim": 5},
            )
        )
    assert KEYWORD_INDEX in plan


def test_exact_ties_break_by_server_then_tool_name_not_random_id(
    db: sessionmaker[Session],
) -> None:
    """Ids are random UUIDs minted per catalog; tie-breaking on them made the
    synthetic eval non-reproducible (top-1 0.79-0.83 across identical runs)."""
    emb = FakeHashEmbedder()
    with db() as s:
        a = add_server(s, "aaa")
        b = add_server(s, "bbb")
        # id order deliberately contradicts name order
        add_tool(s, b, "lookup", "Lookup records", embedder=emb, id="0" * 36)
        add_tool(s, a, "lookup", "Lookup records", embedder=emb, id="f" * 36)
        s.commit()
    got = HybridRetriever(db, emb).retrieve("lookup records", limit=2)
    assert [c.server_name for c in got] == ["aaa", "bbb"]
