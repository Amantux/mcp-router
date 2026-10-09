"""Columns adopted into models.py at integration: discovery writes tool
title/annotations, classifiers record their provenance, dedup review records
who resolved a suggestion — and the credentials table lives in models.py."""

from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.dedup.review import accept_suggestion, dismiss_suggestion
from mcprouter.discovery import apply_listing
from mcprouter.discovery.credentials import ServerCredentialRecord as ReExported
from mcprouter.mcpclient import ToolDescriptor
from mcprouter.models import DuplicateSuggestion, MCPToolRecord, ServerCredentialRecord
from mcprouter.registry.catalog import ClassificationUpdate, auto_classify, update_classification
from mcprouter.registry.classify import RuleBasedClassifier
from tests.support.registry_fixtures import make_server, make_tool

from .conftest import requires_db

SF = sessionmaker[Session]


def test_credentials_table_is_the_models_definition() -> None:
    assert ReExported is ServerCredentialRecord
    assert ServerCredentialRecord.__tablename__ == "mcp_server_credentials"


@requires_db
def test_init_creates_side_tables(db: SF) -> None:
    with db() as s:
        names = set(inspect(s.get_bind()).get_table_names())
    assert {"approval_requests", "eval_results", "mcp_server_credentials"} <= names


@requires_db
def test_discovery_stores_title_and_annotations(db: SF) -> None:
    with db() as s:
        srv = make_server(s)
        d = ToolDescriptor(
            name="read_file",
            description="Read a file",
            input_schema={"type": "object"},
            title="Read file",
            annotations={"readOnlyHint": True},
        )
        apply_listing(s, srv.id, [d], None)
        s.commit()
        tool = s.query(MCPToolRecord).filter_by(server_id=srv.id).one()
        assert tool.title == "Read file"
        assert tool.annotations == {"readOnlyHint": True}


@requires_db
def test_classification_source_records_who_classified(db: SF) -> None:
    with db() as s:
        srv = make_server(s)
        auto = make_tool(s, srv, "read_file", "Read the contents of a file")
        human = make_tool(s, srv, "write_file", "Write a file")
        s.flush()
        auto_classify(s, RuleBasedClassifier(), [auto.id])
        update_classification(s, human.id, ClassificationUpdate(fields={}), actor="alice")
        s.commit()
        s.expire_all()
        assert s.get(MCPToolRecord, auto.id).classification_source == "rules-v1"  # type: ignore[union-attr]
        assert s.get(MCPToolRecord, human.id).classification_source == "human"  # type: ignore[union-attr]


@requires_db
def test_dedup_resolution_columns(db: SF) -> None:
    with db() as s:
        a = DuplicateSuggestion(tool_a_id="a", tool_b_id="b", similarity=0.9)
        b = DuplicateSuggestion(tool_a_id="c", tool_b_id="d", similarity=0.9)
        s.add_all([a, b])
        s.flush()
        accept_suggestion(s, a.id, actor="alice")
        dismiss_suggestion(s, b.id, justification="different auth", actor="bob")
        s.commit()
        assert (a.resolved_by, a.resolution_note) == ("alice", None)
        assert a.resolved_at is not None
        assert (b.resolved_by, b.resolution_note) == ("bob", "different auth")
