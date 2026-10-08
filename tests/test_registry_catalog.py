"""Catalog service: FTS search/filter/pagination, wire shape, classification
review + the reviewed-record guard, enable/disable, usage stats."""

from __future__ import annotations

import math

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.models import MCPToolRecord
from mcprouter.registry.catalog import (
    ClassificationUpdate,
    ToolFilter,
    apply_auto_classification,
    auto_classify,
    get_tool_detail,
    search_tools,
    set_enabled,
    update_classification,
)
from mcprouter.registry.classify import Classification, RuleBasedClassifier
from mcprouter.registry.errors import InvalidArgument, ToolNotFound
from mcprouter.registry.schema import init_registry
from mcprouter.registry.stats import EMA_ALPHA, record_execution
from mcprouter.registry.wire import tool_out

from .conftest import requires_db
from .test_registry_fixtures import make_server, make_tool

pytestmark = requires_db


@pytest.fixture()
def seeded(db: sessionmaker[Session]) -> dict[str, str]:
    init_registry(db.kw["bind"])
    with db() as s:
        gh = make_server(s, "github")
        sl = make_server(s, "slack")
        ids = {
            "list_issues": make_tool(
                s,
                gh,
                "list_issues",
                "List issues in a GitHub repository",
                domain="development",
                operation="read",
                tags=["issues", "tracker"],
                call_count=50,
            ).id,
            "create_issue": make_tool(
                s,
                gh,
                "createIssue",
                "Create a new issue",
                domain="development",
                operation="write",
                tags=["issues"],
            ).id,
            "send_message": make_tool(
                s,
                sl,
                "send_message",
                "Post a message to a Slack channel; mentions issues in passing",
                domain="communication",
                operation="write",
                tags=["chat"],
                enabled=False,
            ).id,
            "search_messages": make_tool(
                s,
                sl,
                "search_messages",
                "Search Slack message history",
                domain="communication",
                operation="read",
                available=False,
                classification_reviewed=True,
            ).id,
        }
        s.commit()
    return ids


def _names(page: object) -> list[str]:
    return [hit.tool.name for hit in page.items]  # type: ignore[attr-defined]


# ----------------------------------------------------------------- search
def test_fts_ranks_name_hits_above_incidental_description_hits(
    db: sessionmaker[Session], seeded: dict[str, str]
) -> None:
    with db() as s:
        page = search_tools(s, ToolFilter(q="issues"))
    names = _names(page)
    assert set(names) == {"list_issues", "createIssue", "send_message"}
    # Name+description+tag hit beats a passing mention in a description.
    assert names[-1] == "send_message"
    assert all(hit.rank is not None and hit.rank > 0 for hit in page.items)
    ranks = [hit.rank for hit in page.items]
    assert ranks == sorted(ranks, reverse=True)


def test_fts_camelcase_name_and_any_term_recall(
    db: sessionmaker[Session], seeded: dict[str, str]
) -> None:
    with db() as s:
        # 'createIssue' is split for indexing.
        assert "createIssue" in _names(search_tools(s, ToolFilter(q="create")))
        # OR semantics: one matching term is enough; more matching terms rank higher.
        names = _names(search_tools(s, ToolFilter(q="slack history nonsenseword")))
    assert names[0] == "search_messages"


def test_filters(db: sessionmaker[Session], seeded: dict[str, str]) -> None:
    with db() as s:
        assert _names(search_tools(s, ToolFilter(domain="communication", enabled=True))) == [
            "search_messages"
        ]
        assert set(_names(search_tools(s, ToolFilter(operation="write")))) == {
            "createIssue",
            "send_message",
        }
        assert set(_names(search_tools(s, ToolFilter(tags=["issues"])))) == {
            "list_issues",
            "createIssue",
        }
        assert _names(search_tools(s, ToolFilter(tags=["issues", "tracker"]))) == ["list_issues"]
        assert _names(search_tools(s, ToolFilter(available=False))) == ["search_messages"]
        assert _names(search_tools(s, ToolFilter(classification_reviewed=True))) == [
            "search_messages"
        ]
        srv = s.get(MCPToolRecord, seeded["send_message"])
        assert srv is not None
        assert set(_names(search_tools(s, ToolFilter(server_id=srv.server_id)))) == {
            "send_message",
            "search_messages",
        }


def test_pagination_total_and_stable_order(
    db: sessionmaker[Session], seeded: dict[str, str]
) -> None:
    with db() as s:
        p1 = search_tools(s, ToolFilter(limit=3, offset=0))
        p2 = search_tools(s, ToolFilter(limit=3, offset=3))
    assert p1.total == p2.total == 4
    assert len(p1.items) == 3 and len(p2.items) == 1
    assert not set(_names(p1)) & set(_names(p2))


def test_invalid_filter_values_rejected(db: sessionmaker[Session]) -> None:
    with db() as s:
        with pytest.raises(InvalidArgument):
            search_tools(s, ToolFilter(operation="delete"))
        with pytest.raises(InvalidArgument):
            search_tools(s, ToolFilter(limit=0))


def test_fts_query_uses_the_gin_expression_index(
    db: sessionmaker[Session], seeded: dict[str, str]
) -> None:
    """Guards drift between the DDL expression and the query expression."""
    from mcprouter.registry.catalog import _fts_where_sql  # noqa: PLC2701

    with db() as s:
        s.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            r[0]
            for r in s.execute(
                text(f"EXPLAIN SELECT id FROM mcp_tools WHERE {_fts_where_sql()}"),
                {"q": "issues"},
            )
        )
    assert "ix_tools_fts" in plan


# ------------------------------------------------------------------ wire
def test_wire_shape_is_camelcase_and_maps_categories_to_capabilities(
    db: sessionmaker[Session], seeded: dict[str, str]
) -> None:
    with db() as s:
        tool = s.get(MCPToolRecord, seeded["list_issues"])
        assert tool is not None
        tool.capabilities = ["issue-tracking"]
        out = tool_out(tool, server_name="github").model_dump(by_alias=True)
    for key in (
        "id",
        "serverId",
        "serverName",
        "name",
        "description",
        "inputSchema",
        "schemaHash",
        "categories",
        "tags",
        "operation",
        "requiredScopes",
        "enabled",
        "version",
        "classificationReviewed",
        "available",
        "stats",
    ):
        assert key in out
    assert out["categories"] == ["issue-tracking"]
    assert "capabilities" not in out
    assert out["stats"]["callCount"] == 50


# -------------------------------------------------------- classification
def test_human_override_sets_reviewed_and_only_supplied_fields(
    db: sessionmaker[Session], seeded: dict[str, str]
) -> None:
    tid = seeded["list_issues"]
    with db() as s:
        update_classification(
            s,
            tid,
            ClassificationUpdate(fields={"operation": "write", "categories": ["triage"]}),
            actor="alice",
        )
        s.commit()
    with db() as s:
        t = s.get(MCPToolRecord, tid)
        assert t is not None
        assert t.classification_reviewed is True
        assert t.operation == "write"
        assert t.capabilities == ["triage"]
        assert t.domain == "development"  # untouched
        assert t.tags == ["issues", "tracker"]  # untouched


def test_empty_override_is_an_approval(db: sessionmaker[Session], seeded: dict[str, str]) -> None:
    tid = seeded["create_issue"]
    with db() as s:
        update_classification(s, tid, ClassificationUpdate(fields={}), actor="alice")
        s.commit()
        t = s.get(MCPToolRecord, tid)
        assert t is not None and t.classification_reviewed is True


def test_override_unknown_tool(db: sessionmaker[Session]) -> None:
    with db() as s, pytest.raises(ToolNotFound):
        update_classification(s, "nope", ClassificationUpdate(fields={}), actor="a")


def test_auto_classification_never_overwrites_a_reviewed_record(
    db: sessionmaker[Session], seeded: dict[str, str]
) -> None:
    tid = seeded["search_messages"]  # classification_reviewed=True
    with db() as s:
        changed = apply_auto_classification(
            s, tid, Classification(operation="execute", domain="development")
        )
        s.commit()
    assert changed is False
    with db() as s:
        t = s.get(MCPToolRecord, tid)
        assert t is not None
        assert (t.operation, t.domain) == ("read", "communication")


def test_reviewed_guard_holds_against_a_stale_reader(
    db: sessionmaker[Session], seeded: dict[str, str]
) -> None:
    """A classifier that read the tool BEFORE a human reviewed it must still
    lose: the guard lives in the UPDATE's WHERE, not in a Python check."""
    tid = seeded["list_issues"]
    with db() as worker:
        stale = worker.get(MCPToolRecord, tid)
        assert stale is not None and stale.classification_reviewed is False
        with db() as human:
            update_classification(
                human, tid, ClassificationUpdate(fields={"domain": "productivity"}), actor="bob"
            )
            human.commit()
        changed = apply_auto_classification(
            worker, tid, Classification(operation="execute", domain="development")
        )
        worker.commit()
    assert changed is False
    with db() as s:
        t = s.get(MCPToolRecord, tid)
        assert t is not None
        assert (t.domain, t.operation) == ("productivity", "read")


def test_auto_classify_bulk_skips_reviewed(
    db: sessionmaker[Session], seeded: dict[str, str]
) -> None:
    with db() as s:
        s.execute(
            MCPToolRecord.__table__.update().values(domain=None, operation="unknown")  # type: ignore[attr-defined]
        )
        s.commit()
    with db() as s:
        run = auto_classify(s, RuleBasedClassifier())
        s.commit()
    assert run.classified == 3
    with db() as s:
        by_name = {t.name: t for t in s.scalars(select(MCPToolRecord))}
    assert by_name["list_issues"].operation == "read"
    assert by_name["list_issues"].domain == "development"
    assert by_name["createIssue"].operation == "write"
    # Reviewed record keeps the human's (here: wiped-by-test) values untouched by auto.
    assert by_name["search_messages"].operation == "unknown"
    assert by_name["search_messages"].classification_reviewed is True


def test_auto_classification_keeps_capabilities_when_classifier_supplies_none(
    db: sessionmaker[Session], seeded: dict[str, str]
) -> None:
    tid = seeded["create_issue"]
    with db() as s:
        t = s.get(MCPToolRecord, tid)
        assert t is not None
        t.capabilities = ["issue-tracking"]
        s.commit()
    with db() as s:
        assert apply_auto_classification(s, tid, Classification(operation="write", domain=None))
        s.commit()
        t = s.get(MCPToolRecord, tid)
        assert t is not None and t.capabilities == ["issue-tracking"]


# ----------------------------------------------------------- enable/detail
def test_set_enabled_and_detail_versions(db: sessionmaker[Session], seeded: dict[str, str]) -> None:
    from mcprouter.models import ToolVersionRecord

    tid = seeded["send_message"]
    with db() as s:
        for v in (1, 2):
            s.add(
                ToolVersionRecord(
                    tool_id=tid,
                    version=v,
                    schema_hash=f"h{v}",
                    snapshot={"v": v},
                    change_kind="schema",
                )
            )
        set_enabled(s, tid, True, actor="alice")
        s.commit()
    with db() as s:
        detail = get_tool_detail(s, tid)
    assert detail.tool.enabled is True
    assert [v.version for v in detail.versions] == [2, 1]
    with db() as s, pytest.raises(ToolNotFound):
        set_enabled(s, "missing", False, actor="a")


# ------------------------------------------------------------------ stats
def test_record_execution_ema(db: sessionmaker[Session], seeded: dict[str, str]) -> None:
    tid = seeded["create_issue"]
    with db() as s:
        assert record_execution(s, tid, ok=True, latency_ms=100.0)
        assert record_execution(s, tid, ok=False, latency_ms=200.0)
        s.commit()
        t = s.get(MCPToolRecord, tid)
        assert t is not None
        s.refresh(t)
    assert (t.call_count, t.error_count) == (2, 1)
    assert t.avg_latency_ms == pytest.approx((1 - EMA_ALPHA) * 100.0 + EMA_ALPHA * 200.0)
    assert EMA_ALPHA == 0.2


def test_record_execution_unknown_tool_and_bad_latency(
    db: sessionmaker[Session], seeded: dict[str, str]
) -> None:
    with db() as s:
        assert record_execution(s, "missing", ok=True, latency_ms=1.0) is False
        for bad in (-1.0, math.nan, math.inf):
            with pytest.raises(InvalidArgument):
                record_execution(s, seeded["create_issue"], ok=True, latency_ms=bad)
