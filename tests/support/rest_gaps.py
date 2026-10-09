"""Shared builders for the E2 REST-gap tests (not a test module)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcprouter.api import deps_auth
from mcprouter.api.deps_auth import configure_security
from mcprouter.api.errors import install_error_handlers
from mcprouter.api.routes_skill_sources import router as src_router
from mcprouter.api.routes_skills import router as skills_router
from mcprouter.settings import Settings

ADMIN = "admin_" + "g" * 40
H = {"Authorization": f"Bearer {ADMIN}"}


def skills_admin_client(db: Any, settings: Settings) -> TestClient:
    """Partial app: skill-sources + skills admin routers, admin token set."""
    deps_auth._reset_dev_warning_for_tests()
    app = FastAPI()
    install_error_handlers(app)
    app.state.settings, app.state.session_factory = settings, db
    app.state.engine = db.kw["bind"]
    configure_security(app, {"MCPR_ADMIN_TOKEN": ADMIN})
    app.include_router(src_router)
    app.include_router(skills_router)
    return TestClient(app)


def write_skill(root: Path, name: str, *, scripts: bool = False) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\nbody\n")
    if scripts:
        (d / "scripts").mkdir()
        (d / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n")


def add_directory_source(c: TestClient, name: str, root: Path) -> str:
    r = c.post(
        "/api/v1/skill-sources",
        json={"name": name, "kind": "directory", "location": str(root)},
        headers=H,
    )
    assert r.status_code == 201, r.text
    sid: str = r.json()["id"]
    assert c.post(f"/api/v1/skill-sources/{sid}/sync", headers=H).status_code == 200
    return sid
