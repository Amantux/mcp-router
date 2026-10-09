"""REST gaps on /skills and /skill-sources (wave-6 E2: P-202, P-203, P-209)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from mcprouter.models import SkillSourceRecord
from mcprouter.settings import Settings
from tests.support.rest_gaps import H, add_directory_source, skills_admin_client, write_skill

from .conftest import requires_db

pytestmark = requires_db


@pytest.fixture()
def client(db: Any, settings: Settings) -> Iterator[TestClient]:
    c = skills_admin_client(db, settings)
    try:
        yield c
    finally:
        with db() as s:
            s.execute(delete(SkillSourceRecord))
            s.commit()


@pytest.fixture()
def seeded(client: TestClient, tmp_path: Path) -> dict[str, str]:
    """Two sources: `one` = alpha (scripts) + beta; `two` = gamma."""
    write_skill(tmp_path / "one", "alpha", scripts=True)
    write_skill(tmp_path / "one", "beta")
    write_skill(tmp_path / "two", "gamma")
    return {
        "one": add_directory_source(client, "one", tmp_path / "one"),
        "two": add_directory_source(client, "two", tmp_path / "two"),
    }


def _names(c: TestClient, **params: str) -> list[str]:
    r = c.get("/api/v1/skills", params=params, headers=H)
    assert r.status_code == 200, r.text
    return sorted(i["name"] for i in r.json()["items"])


# ---- P-202 / D13: sourceId + hasScripts filters ----------------------------
def test_skills_filter_source_id_narrows(client: TestClient, seeded: dict[str, str]) -> None:
    assert _names(client) == ["alpha", "beta", "gamma"]
    assert _names(client, sourceId=seeded["one"]) == ["alpha", "beta"]
    assert _names(client, sourceId=seeded["two"]) == ["gamma"]


def test_skills_filter_source_id_equals_source(client: TestClient, seeded: dict[str, str]) -> None:
    for sid in seeded.values():
        assert _names(client, sourceId=sid) == _names(client, source=sid)


def test_skills_filter_source_and_source_id_disagree_is_422(
    client: TestClient, seeded: dict[str, str]
) -> None:
    r = client.get(
        "/api/v1/skills", params={"source": seeded["one"], "sourceId": seeded["two"]}, headers=H
    )
    assert r.status_code == 422


def test_skills_filter_has_scripts_narrows(client: TestClient, seeded: dict[str, str]) -> None:
    assert _names(client, hasScripts="true") == ["alpha"]
    assert _names(client, hasScripts="false") == ["beta", "gamma"]
