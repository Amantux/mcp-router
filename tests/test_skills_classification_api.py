"""PATCH /api/v1/skills/{id}/classification: admin-only human override that wins."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete

from mcprouter.api import deps_auth
from mcprouter.api.deps_auth import configure_security
from mcprouter.api.routes_skill_sources import router as src_router
from mcprouter.api.routes_skills import router as skills_router
from mcprouter.models import SkillRecord, SkillSourceRecord
from mcprouter.settings import Settings

ADMIN = "admin_" + "c" * 40
H = {"Authorization": f"Bearer {ADMIN}"}


def test_patch_skill_classification(db: Any, settings: Settings, tmp_path: Path) -> None:
    deps_auth._reset_dev_warning_for_tests()
    app = FastAPI()
    app.state.settings, app.state.session_factory = settings, db
    app.state.engine = db.kw["bind"]
    configure_security(app, {"MCPR_ADMIN_TOKEN": ADMIN})
    app.include_router(src_router)
    app.include_router(skills_router)
    d = tmp_path / "alpha"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: alpha\ndescription: Alpha skill\n---\nhello\n")
    c = TestClient(app)
    try:
        body = {"name": "local", "kind": "directory", "location": str(tmp_path)}
        sid = c.post("/api/v1/skill-sources", json=body, headers=H).json()["id"]
        c.post(f"/api/v1/skill-sources/{sid}/sync", headers=H)
        kid = c.get("/api/v1/skills", headers=H).json()["items"][0]["id"]
        url = f"/api/v1/skills/{kid}/classification"
        patch = {"domain": "docs", "categories": ["Writing"], "tags": ["x"]}
        patch |= {"operation": "read", "requiredScopes": ["docs:read"]}
        # Admin gate: no bearer and a non-admin bearer are both refused.
        assert c.patch(url, json=patch).status_code in (401, 403)
        bad = {"Authorization": "Bearer agent_" + "d" * 40}
        assert c.patch(url, json=patch, headers=bad).status_code in (401, 403)
        assert c.patch(url, json={"operation": "nope"}, headers=H).status_code == 422
        assert c.patch(url, json={"bogus": 1}, headers=H).status_code == 422
        r = c.patch(url, json=patch, headers=H)
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["classificationReviewed"] is True
        assert out["domain"] == "docs" and out["operation"] == "read"
        assert out["requiredScopes"] == ["docs:read"] and out["tags"] == ["x"]
        # Human override wins: a re-sync's auto-classification must not undo it.
        c.post(f"/api/v1/skill-sources/{sid}/sync", headers=H)
        with db() as s:
            rec = s.get(SkillRecord, kid)
            assert rec is not None
            assert (rec.domain, rec.operation, rec.classification_source) == (
                "docs",
                "read",
                "human",
            )
        # Empty body approves as-is and still marks reviewed.
        assert c.patch(url, json={}, headers=H).json()["domain"] == "docs"
    finally:
        with db() as s:
            s.execute(delete(SkillSourceRecord))
            s.commit()
