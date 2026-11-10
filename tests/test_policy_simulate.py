"""Tests for policy dry-run simulation (FR-07)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api import deps_auth
from mcprouter.api.deps_auth import configure_security
from mcprouter.api.routes_policy import router
from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.models import ExecutionRecord
from mcprouter.settings import Settings
from tests.support.execution import (
    KEYS,
    Catalog,
    FakeInvoker,
    add_rule,
    seed,
)

from .conftest import TEST_DB_URL, requires_db

pytestmark = requires_db

ADMIN = "admin_" + "q" * 40
H_ADMIN = {"Authorization": f"Bearer {ADMIN}"}


@pytest.fixture()
def env(
    sec_db: sessionmaker[Session],
) -> Iterator[tuple[TestClient, FakeInvoker, Catalog]]:
    deps_auth._reset_dev_warning_for_tests()
    cat = seed(
        sec_db,
        [
            ("github", "list_issues", "read"),
            ("github", "create_issue", "write"),
            ("shell", "run", "execute"),
        ],
    )
    app = FastAPI()
    app.state.settings = Settings(database_url=TEST_DB_URL)
    app.state.engine = sec_db.kw["bind"]
    app.state.session_factory = sec_db
    configure_security(app, {"MCPR_ADMIN_TOKEN": ADMIN})
    inv = FakeInvoker()
    app.state.execution_manager = ExecutionManager(
        sec_db, inv, timeout_s=2.0, limiter=SlidingWindowLimiter(100)
    )
    app.include_router(router)
    yield TestClient(app), inv, cat


def _agent(name: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {KEYS[name]}"}


# ----------------------------------------------------------------- dry-run tests


def test_dry_run_no_candidate_returns_empty_deltas(
    env: tuple[TestClient, FakeInvoker, Catalog],
) -> None:
    """A dry-run with no candidate rule fields returns empty deltas."""
    c, _, _ = env
    body = {"agentId": "alice"}
    r = c.post("/api/v1/policy-rules/dry-run", json=body, headers=H_ADMIN)
    assert r.status_code == 200
    data = r.json()
    assert data["newly_permitted"] == []
    assert data["approval_added"] == []
    assert data["recent"]["attempts"] == 0


def test_dry_run_with_broad_candidate(
    env: tuple[TestClient, FakeInvoker, Catalog],
) -> None:
    """A broad candidate rule surfaces all newly permitted pairs."""
    c, _, cat = env
    # Alice has no rules yet - dry-run a broad "allow all github tools" rule
    body = {
        "agentId": "alice",
        "serverId": cat.servers["github"].id,
        "toolName": "*",
        "maxOperation": "execute",
        "requiresApproval": False,
    }
    r = c.post("/api/v1/policy-rules/dry-run", json=body, headers=H_ADMIN)
    assert r.status_code == 200
    data = r.json()
    # Should permit list_issues and create_issue (both github tools)
    assert len(data["newly_permitted"]) == 2
    # shell.run is from a different server, should not be in newly_permitted
    github_tool_ids = {f"tool:{cat.tools['github.list_issues'].id}", f"tool:{cat.tools['github.create_issue'].id}"}
    assert set(d["id"] for d in data["newly_permitted"]) == github_tool_ids


def test_dry_run_sticky_approval(
    env: tuple[TestClient, FakeInvoker, Catalog], sec_db: sessionmaker[Session]
) -> None:
    """Already-permitted pairs under a candidate requiring approval appear in approval_added."""
    c, _, cat = env
    # Add an existing rule that allows alice all github tools without approval
    add_rule(sec_db, "alice", server_id=cat.servers["github"].id, tool_name="*", max_operation="execute")

    # Dry-run a candidate that requires approval for the same tools
    body = {
        "agentId": "alice",
        "serverId": cat.servers["github"].id,
        "toolName": "*",
        "maxOperation": "execute",
        "requiresApproval": True,
    }
    r = c.post("/api/v1/policy-rules/dry-run", json=body, headers=H_ADMIN)
    assert r.status_code == 200
    data = r.json()
    # The tools are already permitted, but would require approval now
    assert len(data["approval_added"]) == 2
    # Should NOT appear in newly_permitted (they're already allowed)
    assert data["newly_permitted"] == []


def test_dry_run_unknown_operation_escapes_to_execute(
    env: tuple[TestClient, FakeInvoker, Catalog], sec_db: sessionmaker[Session]
) -> None:
    """Unknown/unclassified operations are treated as execute (fail-closed)."""
    c, _, cat = env
    # Create a tool with unknown operation (simulating misclassification)
    from mcprouter.models import MCPToolRecord
    with sec_db() as s:
        srv = cat.servers["shell"]
        unknown_tool = MCPToolRecord(
            server_id=srv.id,
            name="unknown_tool",
            description="Unknown operation tool",
            input_schema={"type": "object"},
            schema_hash="dummy_hash",
            operation="unknown",  # Unknown operation
        )
        s.add(unknown_tool)
        s.commit()

        # Add a rule that only allows read operations
        add_rule(sec_db, "alice", server_id=srv.id, tool_name="run", max_operation="read")

    # Dry-run a candidate that allows execute on shell server
    body = {
        "agentId": "alice",
        "serverId": cat.servers["shell"].id,
        "toolName": "*",
        "maxOperation": "execute",
        "requiresApproval": False,
    }
    r = c.post("/api/v1/policy-rules/dry-run", json=body, headers=H_ADMIN)
    assert r.status_code == 200
    data = r.json()
    # unknown_tool has operation="unknown" -> treated as execute
    # run has operation="execute"
    # Both should be in newly_permitted since the candidate allows execute
    assert len(data["newly_permitted"]) == 2


def test_dry_run_recent_history_counts(
    env: tuple[TestClient, FakeInvoker, Catalog], sec_db: sessionmaker[Session]
) -> None:
    """Recent history counts reproduce manual counts over the same window."""
    c, _, cat = env

    # Add some execution records for alice
    from datetime import datetime, timedelta

    with sec_db() as s:
        # Add 3 execution records (all denied - no rules exist)
        for i in range(3):
            rec = ExecutionRecord(
                agent_id="alice",
                tool_id=cat.tools["github.list_issues"].id,
                server_id=cat.servers["github"].id,
                outcome="denied",
                detail="no matching policy rule",
                created_at=datetime.now(UTC) - timedelta(hours=i * 10),
            )
            s.add(rec)
        s.commit()

    # Dry-run a candidate that would permit all github tools
    body = {
        "agentId": "alice",
        "serverId": cat.servers["github"].id,
        "toolName": "*",
        "maxOperation": "execute",
        "requiresApproval": False,
    }
    r = c.post("/api/v1/policy-rules/dry-run", json=body, headers=H_ADMIN)
    assert r.status_code == 200
    data = r.json()

    # Should show 3 attempts, all would be permitted now
    assert data["recent"]["attempts"] == 3
    assert data["recent"]["would_permit"] == 3
    assert data["recent"]["of_which_approval"] == 0
    # All 3 were denied before, now would be permitted
    assert data["recent"]["currently_denied_now_permitted"] == 3


def test_dry_run_requires_admin(
    env: tuple[TestClient, FakeInvoker, Catalog],
) -> None:
    """Dry-run endpoint requires admin authentication."""
    c, _, _ = env
    body = {"agentId": "alice"}
    # Anonymous request
    r = c.post("/api/v1/policy-rules/dry-run", json=body)
    assert r.status_code == 401

    # Agent request (not admin)
    r = c.post("/api/v1/policy-rules/dry-run", json=body, headers=_agent("alice"))
    assert r.status_code == 401


def test_dry_run_agent_not_found(
    env: tuple[TestClient, FakeInvoker, Catalog],
) -> None:
    """Dry-run with unknown agent_id returns 404."""
    c, _, _ = env
    body = {"agentId": "nonexistent_agent"}
    r = c.post("/api/v1/policy-rules/dry-run", json=body, headers=H_ADMIN)
    assert r.status_code == 404


def test_dry_run_with_skill_resource_kind(
    env: tuple[TestClient, FakeInvoker, Catalog], sec_db: sessionmaker[Session]
) -> None:
    """Dry-run works with resource_kind='skill'."""
    c, _, cat = env

    # Add a skill source and skill
    from mcprouter.models import SkillRecord, SkillSourceRecord

    with sec_db() as s:
        src = SkillSourceRecord(
            name="test_source",
            kind="directory",
            location="/tmp/test_skills",
        )
        s.add(src)
        s.flush()

        skill = SkillRecord(
            source_id=src.id,
            name="test_skill",
            description="A test skill",
            body="# Test skill\n",
            relative_path="test_skill/SKILL.md",
            license="MIT",
            operation="read",
        )
        s.add(skill)
        s.commit()

        # Add a rule that allows the skill
        add_rule(sec_db, "alice", server_id=src.id, tool_name="test_skill", max_operation="read")

    # Dry-run a candidate that would require approval for the skill
    body = {
        "agentId": "alice",
        "serverId": src.id,
        "toolName": "test_skill",
        "maxOperation": "read",
        "requiresApproval": True,
        "resourceKind": "skill",
    }
    r = c.post("/api/v1/policy-rules/dry-run", json=body, headers=H_ADMIN)
    assert r.status_code == 200
    data = r.json()
    # The skill is already permitted, but would require approval
    assert len(data["approval_added"]) == 1
    assert data["approval_added"][0]["id"].startswith("skill:")
    assert data["newly_permitted"] == []


def test_dry_run_no_duplicate_policy_generation(
    env: tuple[TestClient, FakeInvoker, Catalog], sec_db: sessionmaker[Session]
) -> None:
    """Dry-run does not bump policy generation."""
    c, _, cat = env

    # Get initial policy generation count
    from mcprouter.generation import policy_generation

    initial_version = policy_generation()

    # Perform dry-run
    body = {
        "agentId": "alice",
        "serverId": cat.servers["github"].id,
        "toolName": "*",
        "maxOperation": "execute",
        "requiresApproval": False,
    }
    r = c.post("/api/v1/policy-rules/dry-run", json=body, headers=H_ADMIN)
    assert r.status_code == 200

    # Policy generation should not have changed
    final_version = policy_generation()
    assert final_version == initial_version
