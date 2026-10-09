"""Git sync failures surface constant messages, never the exception text."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete

from mcprouter.api import deps_auth
from mcprouter.api.deps_auth import configure_security
from mcprouter.api.routes_skill_sources import router as src_router
from mcprouter.models import SkillSourceRecord
from mcprouter.settings import Settings
from mcprouter.skills import gitsource, sources

ADMIN = "admin_" + "f" * 40
H = {"Authorization": f"Bearer {ADMIN}"}
SECRET = "fatal: could not read from https://user:hunter2@git.example/r /var/cache/x"

CASES = [
    (gitsource.GitCloneFailedError, "git clone failed"),
    (gitsource.GitTimeoutError, "git clone timed out"),
    (gitsource.GitTooLargeError, "repository too large"),
    (gitsource.GitInvalidRefError, "invalid git ref"),
    (gitsource.GitNotRepositoryError, "not a git repository"),
    (gitsource.GitSourceError, "git sync failed"),
]


@pytest.mark.parametrize(("cls", "expected"), CASES)
def test_sync_error_body_is_curated(
    db: Any,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cls: type[gitsource.GitSourceError],
    expected: str,
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise cls(SECRET)

    monkeypatch.setattr(sources.gitsource, "fetch", boom)
    deps_auth._reset_dev_warning_for_tests()
    app = FastAPI()
    app.state.settings, app.state.session_factory = settings, db
    app.state.engine = db.kw["bind"]
    configure_security(app, {"MCPR_ADMIN_TOKEN": ADMIN})
    app.include_router(src_router)
    c = TestClient(app)
    try:
        body = {"name": "g", "kind": "git", "location": "https://git.example/r"}
        sid = c.post("/api/v1/skill-sources", json=body, headers=H).json()["id"]
        with caplog.at_level(logging.WARNING):
            r = c.post(f"/api/v1/skill-sources/{sid}/sync", headers=H)
        assert r.json()["error"] == expected
        assert "hunter2" not in r.text and "fatal" not in r.text
        assert "hunter2" not in caplog.text
        assert cls.__name__ in caplog.text
    finally:
        with db() as s:
            s.execute(delete(SkillSourceRecord))
            s.commit()


def test_fetch_without_git_dir_is_not_a_repository(tmp_path: Path) -> None:
    def runner(argv: Any) -> None:
        Path(argv[-1]).mkdir(parents=True)

    with pytest.raises(gitsource.GitNotRepositoryError):
        gitsource.fetch("abc", "https://example.com/r.git", "main", tmp_path, runner=runner)
