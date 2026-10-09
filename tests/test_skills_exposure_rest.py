"""Wave-4 S3: REST activation / resource / bundle routes. Mutation-checked:
agentId-required-for-admin, impersonation audit, cross-agent 403, server-side
visibility, traversal refusal, bundle route not shadowed."""

from __future__ import annotations

import hashlib
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
from mcprouter.api.routes_skills import UNKNOWN_SKILL
from mcprouter.api.routes_skills import agent_router as router
from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.gateway.skills import SkillExposure
from mcprouter.models import ExecutionRecord, RoutingDecisionRecord, SkillRecord
from mcprouter.settings import Settings
from mcprouter.skills.serve import manifest_entries

from .conftest import TEST_DB_URL, requires_db
from .test_execution_support import KEYS, FakeInvoker, seed
from .test_execution_support import sec_db_fixture as sec_db_fixture  # registers fixture
from .test_skills_exposure_activation import FakePolicy, _clone, _seed

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
    [
        "..%2f..%2fetc%2fpasswd",
        "guide.md%00.txt",
        "%2fetc%2fpasswd",
        "%2e%2e/secret",
        "..%2fsecret",
        "link.md",
        "missing.md",
    ],
)
def test_traversal_refused_without_leak(env: Env, path: str, tmp_path: Path) -> None:
    # The escapes ARE in the manifest with the right sha256, so `not_in_manifest`
    # cannot be what refuses them: removing the normalizer (../secret) or the
    # realpath containment guard (link.md -> outside) must make this test fail.
    (tmp_path / "secret").write_text("TOPSECRET")
    (tmp_path / "src" / "secret").write_text("TOPSECRET")  # = pdf-tools/../secret
    (tmp_path / "src" / "pdf-tools" / "link.md").symlink_to(tmp_path / "secret")
    digest = hashlib.sha256(b"TOPSECRET").hexdigest()
    with env.db() as s:
        sk = s.get(SkillRecord, env.a)
        assert sk is not None
        sk.resource_manifest = [
            *(sk.resource_manifest or []),
            {"path": "../secret", "size": 9, "sha256": digest},
            {"path": "link.md", "size": 9, "sha256": digest},
        ]
        s.commit()
        assert "../secret" not in manifest_entries(sk)  # normalizer drops it
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
    assert all((x.agent_id, x.outcome) == ("alice", "bundle") for x in rows)
    with env.db() as s:
        assert (
            s.get(SkillRecord, env.a).activation_count == 0
        )  # bundle never activates; type: ignore[union-attr]


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
    assert r.status_code == 404
    # Owner decision: unrouted access is audited as a denial, never as access.
    assert [(x.outcome, x.detail, x.skill_id) for x in env.rows()] == [
        ("denied", "not routed", None)
    ]


def test_stale_resource_is_409(env: Env, tmp_path: Path) -> None:
    (tmp_path / "src" / "pdf-tools" / "guide.md").write_text("# Gxide")  # same size, new hash
    env.route("alice", env.a)
    r = env.client.get(f"/api/v1/skills/{env.a}/resources/guide.md", headers=H_ALICE)
    assert (r.status_code, r.json()["detail"]) == (
        409,
        "Skill resource is out of date; re-index the skill.",
    )


def test_bundle_too_many_is_413(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import functools

    from mcprouter.gateway import skills as gw
    from mcprouter.skills.bundle import _build_bundle

    monkeypatch.setattr(gw, "_build_bundle", functools.partial(_build_bundle, max_skills=1))
    env.route("alice", env.a, env.b)
    r = env.client.get("/api/v1/skills/bundle", headers=H_ALICE)
    assert r.status_code == 413, r.text
    assert [(x.outcome, x.detail) for x in env.rows()] == [("error", "bundle: too_many")]


def test_bundle_duplicate_names_is_409(env: Env) -> None:
    dup = _clone(env.db, env.a, "pdf-tools", new_source=True)
    env.route("alice", env.a, dup)
    r = env.client.get("/api/v1/skills/bundle", headers=H_ALICE)
    assert r.status_code == 409, r.text
    assert "share a name" in r.json()["detail"]


def test_admin_cannot_impersonate_disabled_agent(env: Env) -> None:
    from sqlalchemy import update

    from mcprouter.api.routes_execute import AGENT_DISABLED
    from mcprouter.models import AgentPrincipal

    env.route("alice", env.a)
    with env.db() as s:
        s.execute(
            update(AgentPrincipal).where(AgentPrincipal.agent_id == "alice").values(enabled=False)
        )
        s.commit()
    for r in (
        env.client.post(
            f"/api/v1/skills/{env.a}/activate", json={"agentId": "alice"}, headers=H_ADMIN
        ),
        env.client.get(f"/api/v1/skills/{env.a}/resources/guide.md?agentId=alice", headers=H_ADMIN),
        env.client.get("/api/v1/skills/bundle?agentId=alice", headers=H_ADMIN),
    ):
        assert (r.status_code, r.json()["detail"]) == (403, AGENT_DISABLED), r.text
    assert env.rows() == []
