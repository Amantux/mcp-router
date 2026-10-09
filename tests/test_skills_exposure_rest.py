"""Wave-4 S3: REST activation / resource / bundle routes. Mutation-checked:
agentId-required-for-admin, impersonation audit, cross-agent 403, server-side
visibility, traversal refusal, bundle route not shadowed."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api import deps_auth
from mcprouter.api.deps_auth import configure_security
from mcprouter.api.routes_skills import UNKNOWN_SKILL, router
from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.gateway.skills import SkillExposure
from mcprouter.models import ExecutionRecord, RoutingDecisionRecord, SkillRecord
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db
from .test_execution_support import KEYS, FakeInvoker, seed
from .test_execution_support import sec_db_fixture as sec_db_fixture  # registers fixture
from .test_skills_exposure_activation import FakePolicy, _seed

pytestmark = requires_db

ADMIN = "admin_" + "e" * 40
H_ADMIN = {"Authorization": f"Bearer {ADMIN}"}
H_ALICE = {"Authorization": f"Bearer {KEYS['alice']}"}
H_BOB = {"Authorization": f"Bearer {KEYS['bob']}"}


class Env:
    def __init__(self, client: TestClient, db: sessionmaker[Session], policy: FakePolicy):
        self.client, self.db, self.policy = client, db, policy
        self.a = self.b = ""

    def route(self, agent: str, *skill_ids: str) -> None:
        with self.db() as s:
            s.add(
                RoutingDecisionRecord(
                    agent_id=agent, query="q", selected_tool_ids=[f"skill:{i}" for i in skill_ids]
                )
            )
            s.commit()

    def rows(self) -> list[ExecutionRecord]:
        with self.db() as s:
            return list(s.scalars(select(ExecutionRecord).order_by(ExecutionRecord.id)).all())


@pytest.fixture()
def env(sec_db: sessionmaker[Session], tmp_path: Path) -> Iterator[Env]:
    seed(sec_db, [])  # principals alice + bob
    deps_auth._reset_dev_warning_for_tests()
    app = FastAPI()
    app.state.settings = Settings(database_url=TEST_DB_URL)
    app.state.engine = sec_db.kw["bind"]
    app.state.session_factory = sec_db
    configure_security(app, {"MCPR_ADMIN_TOKEN": ADMIN})
    mgr = ExecutionManager(sec_db, FakeInvoker(), timeout_s=1.0, limiter=SlidingWindowLimiter(100))
    policy = FakePolicy()
    app.state.skill_exposure = SkillExposure(
        sec_db,
        mgr,
        policy,
        SlidingWindowLimiter(100, 60.0),
        cache_dir=str(tmp_path / "cache"),
        body_max_bytes=65536,
        resource_max_bytes=1024,
    )
    app.include_router(router)  # app.py registration is the integrator's job
    e = Env(TestClient(app), sec_db, policy)
    e.a, e.b = _seed(sec_db, tmp_path / "src")
    yield e


def test_activate_as_agent(env: Env) -> None:
    env.route("alice", env.a)
    r = env.client.post(
        f"/api/v1/skills/{env.a}/activate", json={"routeRequestId": "rr1"}, headers=H_ALICE
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["body"] == "BODY pdf-tools"
    assert out["resources"] == [{"path": "guide.md", "size": 7, "kind": None}]
    [row] = env.rows()
    assert (row.id, row.agent_id, row.outcome, row.initiated_by) == (
        out["recordId"],
        "alice",
        "ok",
        None,
    )
    assert row.route_request_id == "rr1"


def test_visibility_is_server_side_latest_decision(env: Env) -> None:
    env.route("alice", env.a)
    env.route("bob", env.b)  # routed for bob only
    for sid in (env.b, "nope"):
        r = env.client.post(f"/api/v1/skills/{sid}/activate", headers=H_ALICE)
        assert (r.status_code, r.json()["detail"]) == (404, UNKNOWN_SKILL)
    env.route("alice", env.b)  # latest decision replaces the earlier one
    assert env.client.post(f"/api/v1/skills/{env.a}/activate", headers=H_ALICE).status_code == 404
    assert env.client.post(f"/api/v1/skills/{env.b}/activate", headers=H_ALICE).status_code == 200


def test_agent_cannot_name_another_agent(env: Env) -> None:
    env.route("bob", env.a)
    r = env.client.post(
        f"/api/v1/skills/{env.a}/activate", json={"agentId": "bob"}, headers=H_ALICE
    )
    assert r.status_code == 403 and env.rows() == []
    r = env.client.get("/api/v1/skills/bundle?agentId=bob", headers=H_ALICE)
    assert r.status_code == 403


def test_admin_requires_agent_id(env: Env) -> None:
    env.route("alice", env.a)
    for r in (
        env.client.post(f"/api/v1/skills/{env.a}/activate", headers=H_ADMIN),
        env.client.get(f"/api/v1/skills/{env.a}/resources/guide.md", headers=H_ADMIN),
        env.client.get("/api/v1/skills/bundle", headers=H_ADMIN),
    ):
        assert r.status_code == 400, r.text
    assert env.rows() == []
    r = env.client.post(
        f"/api/v1/skills/{env.a}/activate", json={"agentId": "ghost"}, headers=H_ADMIN
    )
    assert r.status_code == 404


def test_admin_impersonation_is_audited(env: Env) -> None:
    env.route("alice", env.a)
    r = env.client.post(
        f"/api/v1/skills/{env.a}/activate",
        json={"agentId": "alice", "routeRequestId": "rr9"},
        headers=H_ADMIN,
    )
    assert r.status_code == 200, r.text
    [row] = env.rows()
    assert (row.agent_id, row.initiated_by, row.route_request_id) == ("alice", "admin", None)


def test_denied_and_rate_limited(env: Env) -> None:
    env.route("alice", env.a)
    env.policy.allow = False
    r = env.client.post(f"/api/v1/skills/{env.a}/activate", headers=H_ALICE)
    assert r.status_code == 403 and "policy" in r.json()["detail"]
    env.policy.allow = True
    env.client.app.state.skill_exposure._limiter = SlidingWindowLimiter(0, 60.0)  # type: ignore[attr-defined]
    assert env.client.post(f"/api/v1/skills/{env.a}/activate", headers=H_ALICE).status_code == 429


def test_resource_read(env: Env) -> None:
    env.route("alice", env.a)
    r = env.client.get(f"/api/v1/skills/{env.a}/resources/guide.md", headers=H_ALICE)
    assert r.status_code == 200, r.text
    assert r.content == b"# Guide" and r.headers["content-type"].startswith("text/")
    assert r.headers["x-content-type-options"] == "nosniff"
    [row] = env.rows()
    assert (row.outcome, row.detail) == ("read", "resource: guide.md")
    r = env.client.get(f"/api/v1/skills/{env.a}/resources/guide.md?agentId=alice", headers=H_ADMIN)
    assert r.status_code == 200, r.text
    assert sorted(str(x.initiated_by) for x in env.rows()) == ["None", "admin"]


@pytest.mark.parametrize(
    "path",
    ["..%2f..%2fetc%2fpasswd", "guide.md%00.txt", "%2fetc%2fpasswd", "%2e%2e/secret", "missing.md"],
)
def test_traversal_refused_without_leak(env: Env, path: str, tmp_path: Path) -> None:
    (tmp_path / "secret").write_text("TOPSECRET")
    env.route("alice", env.a)
    r = env.client.get(f"/api/v1/skills/{env.a}/resources/{path}", headers=H_ALICE)
    assert r.status_code == 404, r.text
    assert r.json() == {"detail": UNKNOWN_SKILL}
    assert "TOPSECRET" not in r.text and "root" not in r.text and str(tmp_path) not in r.text


def test_bundle(env: Env) -> None:
    env.route("alice", env.a, env.b)
    r = env.client.get("/api/v1/skills/bundle", headers=H_ALICE)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert all(not n.startswith("/") and ".." not in n.split("/") for n in names)
    marker = next(n for n in names if n.endswith(".mcp-router-bundle.json"))
    assert set(json.loads(zipfile.ZipFile(io.BytesIO(r.content)).read(marker))["skills"]) == {
        "pdf-tools",
        "other-skill",
    }
    rows = env.rows()
    assert sorted(x.skill_id or "" for x in rows) == sorted([env.a, env.b])
    assert all((x.agent_id, x.outcome) == ("alice", "ok") for x in rows)
    with env.db() as s:
        assert s.get(SkillRecord, env.a).activation_count == 1  # type: ignore[union-attr]


def test_bundle_caps_and_skipped_header(env: Env, tmp_path: Path) -> None:
    big = tmp_path / "src" / "pdf-tools" / "guide.md"
    big.write_text("x" * 5000)  # > resource_max_bytes and sha mismatch -> skipped
    env.route("alice", env.a)
    r = env.client.get("/api/v1/skills/bundle?agentId=alice", headers=H_ADMIN)
    assert r.status_code == 200, r.text
    assert r.headers["x-skipped-resources"] == "pdf-tools/guide.md"
    assert not any(
        n.endswith("guide.md") for n in zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    )
    assert {x.initiated_by for x in env.rows()} == {"admin"}


def test_bundle_nothing_routed_is_404(env: Env) -> None:
    r = env.client.get("/api/v1/skills/bundle", headers=H_ALICE)
    assert (r.status_code, r.json()["detail"]) == (404, UNKNOWN_SKILL)


def test_non_skill_route_entries_grant_nothing(env: Env) -> None:
    # A tool id that is exactly len("skill:") chars + a skill id must not be
    # sliced into visibility: only "skill:"-prefixed entries count.
    with env.db() as s:
        s.add(
            RoutingDecisionRecord(agent_id="alice", query="q", selected_tool_ids=["tool::" + env.b])
        )
        s.commit()
    r = env.client.post(f"/api/v1/skills/{env.b}/activate", headers=H_ALICE)
    assert r.status_code == 404 and env.rows() == []
