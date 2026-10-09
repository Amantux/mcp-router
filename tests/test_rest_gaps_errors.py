"""P-204: the curated 422 (no `input` echo) applies to every /api/ path."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.app import create_app
from mcprouter.settings import Settings

from .conftest import requires_db

pytestmark = requires_db

SENTINEL = "sentinel-" + "q" * 24


@pytest.fixture()
def client(db: sessionmaker[Session], settings: Settings) -> Iterator[TestClient]:
    yield TestClient(create_app(settings, env={}))  # dev mode: admin routes open


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        # Missing `transport`; env carries the secret.
        ("POST", "/api/v1/servers", {"name": "a", "env": {"K": SENTINEL}}),
        ("POST", "/api/v1/servers", {"name": SENTINEL * 10, "transport": "stdio"}),
        ("POST", "/api/v1/policy-rules", {"agentId": "a", "maxOperation": SENTINEL}),
        ("POST", "/api/v1/principals", {"agentId": SENTINEL + " not valid"}),
        ("POST", "/api/v1/skill-sources", {"name": "x", "kind": SENTINEL, "location": "/x"}),
        ("POST", "/api/v1/route", {"query": "", "agent_id": SENTINEL, "nope": SENTINEL}),
    ],
)
def test_validation_error_does_not_echo_input(
    client: TestClient, method: str, path: str, body: dict[str, object]
) -> None:
    r = client.request(method, path, json=body)
    assert r.status_code == 422, r.text
    assert SENTINEL not in r.text
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail
    for err in detail:
        assert set(err) == {"loc", "msg", "type"}  # the /route shape, now everywhere


def test_query_validation_is_curated_too(client: TestClient) -> None:
    r = client.get("/api/v1/tools", params={"limit": SENTINEL})
    assert r.status_code == 422 and SENTINEL not in r.text
