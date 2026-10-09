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


# ---- P-203: skill-source PATCH null / duplicate / userinfo; extra keys -----
@pytest.fixture()
def one_source(client: TestClient, tmp_path: Path) -> str:
    write_skill(tmp_path / "s", "alpha")
    return add_directory_source(client, "src-a", tmp_path / "s")


@pytest.mark.parametrize("field", ["enabled", "name", "location", "syncIntervalS"])
def test_skill_source_patch_null_is_422_and_row_unchanged(
    client: TestClient, one_source: str, field: str
) -> None:
    before = client.get(f"/api/v1/skill-sources/{one_source}", headers=H).json()
    r = client.patch(f"/api/v1/skill-sources/{one_source}", json={field: None}, headers=H)
    assert r.status_code == 422, r.text
    after = client.get(f"/api/v1/skill-sources/{one_source}", headers=H).json()
    assert after == before


def test_skill_source_patch_git_ref_null_is_allowed(client: TestClient, one_source: str) -> None:
    r = client.patch(f"/api/v1/skill-sources/{one_source}", json={"gitRef": None}, headers=H)
    assert r.status_code == 200 and r.json()["gitRef"] is None


def test_skill_source_patch_rename_to_existing_is_409(
    client: TestClient, one_source: str, tmp_path: Path
) -> None:
    write_skill(tmp_path / "t", "beta")
    other = add_directory_source(client, "src-b", tmp_path / "t")
    r = client.patch(f"/api/v1/skill-sources/{other}", json={"name": "src-a"}, headers=H)
    assert r.status_code == 409
    assert r.json()["detail"] == "a skill source with that name exists"
    assert client.get(f"/api/v1/skill-sources/{other}", headers=H).json()["name"] == "src-b"
    # Renaming to its own name is a no-op, not a conflict.
    same = client.patch(f"/api/v1/skill-sources/{other}", json={"name": "src-b"}, headers=H)
    assert same.status_code == 200


@pytest.mark.parametrize(
    "location",
    ["https://u:pw-canary@example.com/r.git", "https://pw-canary@example.com/r.git"],
)
def test_skill_source_git_userinfo_rejected(client: TestClient, location: str) -> None:
    body = {"name": "g", "kind": "git", "location": location}
    r = client.post("/api/v1/skill-sources", json=body, headers=H)
    assert r.status_code == 422
    assert "pw-canary" not in r.text
    listed = client.get("/api/v1/skill-sources", headers=H)
    assert listed.status_code == 200 and "pw-canary" not in listed.text


def test_skill_source_patch_git_userinfo_rejected(client: TestClient) -> None:
    body = {"name": "g", "kind": "git", "location": "https://example.com/r.git"}
    sid = client.post("/api/v1/skill-sources", json=body, headers=H).json()["id"]
    r = client.patch(
        f"/api/v1/skill-sources/{sid}",
        json={"location": "https://u:pw-canary@example.com/r.git"},
        headers=H,
    )
    assert r.status_code == 422 and "pw-canary" not in r.text
    got = client.get(f"/api/v1/skill-sources/{sid}", headers=H).json()
    assert got["location"] == "https://example.com/r.git"


def test_skill_source_kind_is_an_enum(client: TestClient, tmp_path: Path) -> None:
    body = {"name": "x", "kind": "svn", "location": str(tmp_path)}
    assert client.post("/api/v1/skill-sources", json=body, headers=H).status_code == 422
    schema = client.app.openapi()["components"]["schemas"]["SourceIn"]  # type: ignore[attr-defined]
    assert schema["properties"]["kind"]["enum"] == ["directory", "git"]


def test_skill_source_unknown_field_is_422(client: TestClient, tmp_path: Path) -> None:
    body = {"name": "x", "kind": "directory", "location": str(tmp_path), "bogus": 1}
    assert client.post("/api/v1/skill-sources", json=body, headers=H).status_code == 422
