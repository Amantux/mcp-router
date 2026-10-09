"""REST contract gaps (wave-6 E2, P-208): request-schema 422s, approvals
status/limit, pagination bounds on the 5 paginated routes, wire casing."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.app import create_app
from mcprouter.settings import Settings

from .conftest import requires_db

pytestmark = requires_db


@pytest.fixture()
def client(db: sessionmaker[Session], settings: Settings) -> Iterator[TestClient]:
    yield TestClient(create_app(settings, env={}))  # dev mode: admin routes open


# ---- feedback request schema ------------------------------------------------
_ITEM = {"id": "t.a", "helpful": True}


@pytest.mark.parametrize(
    "body",
    [
        {"items": []},  # min_length=1
        {"items": [{**_ITEM, "bogus": 1}]},  # extra="forbid" on items
        {"items": [_ITEM], "bogus": 1},  # extra="forbid" on the envelope
        {"items": [_ITEM] * 51},  # max_length=50
        {"items": [{**_ITEM, "note": "n" * 2001}]},  # note max_length=2000
        {"items": [{"id": "t.a"}]},  # helpful required
    ],
    ids=["empty", "item-extra", "body-extra", "51-items", "2001-note", "no-helpful"],
)
def test_feedback_body_validation_422(client: TestClient, body: dict[str, object]) -> None:
    r = client.post("/api/v1/route/r1/feedback", json=body)
    assert r.status_code == 422, r.text


# ---- approvals --------------------------------------------------------------
def test_approvals_unknown_status_is_422(client: TestClient) -> None:
    assert client.get("/api/v1/approvals", params={"status": "pending"}).status_code == 200
    assert client.get("/api/v1/approvals", params={"status": "bogus"}).status_code == 422


@pytest.mark.parametrize("limit", ["0", "501", "x"])
def test_approvals_limit_bounds(client: TestClient, limit: str) -> None:
    assert client.get("/api/v1/approvals", params={"limit": limit}).status_code == 422


def test_approvals_limit_applies(
    client: TestClient, db: sessionmaker[Session], settings: Settings
) -> None:
    from datetime import UTC, datetime, timedelta

    from mcprouter.execution.models import ApprovalRequest

    now = datetime.now(UTC)
    with db() as s:
        for i in range(3):
            s.add(
                ApprovalRequest(
                    agent_id="a",
                    tool_id=f"t{i}",
                    server_id="s",
                    schema_hash="h",
                    arguments={},
                    summary={},
                    status="pending",
                    created_at=now - timedelta(minutes=i),
                    expires_at=now + timedelta(hours=1),
                )
            )
        s.commit()
    r = client.get("/api/v1/approvals", params={"limit": 2})
    assert r.status_code == 200 and isinstance(r.json(), list)  # shape unchanged
    assert [a["toolId"] for a in r.json()] == ["t0", "t1"]  # newest first, capped
    assert len(client.get("/api/v1/approvals").json()) == 3


# ---- pagination bounds on the 5 paginated routes ----------------------------
PAGINATED = {  # path -> max limit
    "/api/v1/tools": 200,
    "/api/v1/skills": 500,
    "/api/v1/executions": 200,
    "/api/v1/dedup/suggestions": 200,
    "/api/v1/analytics/tools": 500,
}


@pytest.mark.parametrize("path", sorted(PAGINATED))
def test_pagination_bounds(client: TestClient, path: str) -> None:
    mx = PAGINATED[path]
    for params in ({"limit": 0}, {"limit": mx + 1}, {"offset": -1}):
        r = client.get(path, params=params)
        assert r.status_code == 422, (path, params, r.text)
    ok = client.get(path, params={"limit": mx, "offset": 0})
    assert ok.status_code == 200, ok.text
    assert ok.json()["limit"] == mx


# ---- wire casing --------------------------------------------------------------
# The only response schemas allowed to carry snake_case properties. Changing
# their casing would break shipped clients (A1-018: document, don't change).
SNAKE_CASE_SCHEMAS = {
    "RouteResponse": "POST /route: SPEC §9 snake_case body (nests camel bodyTokensEst)",
    "EvaluateResponse": "POST /route/evaluate: snake_case like /route",
}
# Untyped (dict) responses with snake_case keys: no schema to check, listed so
# adding a typed model forces a decision here.
SNAKE_CASE_UNTYPED = {
    ("post", "/api/v1/decision/systemone"): "System One edge contract (input_tokens, ...)",
}


def _style(key: str) -> str:
    return "snake" if "_" in key else "camel" if re.search("[A-Z]", key) else "flat"


def test_openapi_property_casing(client: TestClient) -> None:
    spec = client.app.openapi()  # type: ignore[attr-defined]
    offenders = {}
    for name, schema in spec["components"]["schemas"].items():
        styles = {_style(k) for k in schema.get("properties", {})} - {"flat"}
        if "snake" in styles and name not in SNAKE_CASE_SCHEMAS:
            offenders[name] = sorted(schema["properties"])
    assert not offenders, f"snake_case properties outside the allowlist: {offenders}"
    for name in SNAKE_CASE_SCHEMAS:
        props = spec["components"]["schemas"][name]["properties"]
        assert any(_style(k) == "snake" for k in props), f"{name} is no longer snake: prune it"
    for method, path in SNAKE_CASE_UNTYPED:
        op = spec["paths"][path][method]
        ok = op["responses"].get("200", {}).get("content", {}).get("application/json", {})
        assert "$ref" not in ok.get("schema", {}), f"{path} is typed now: move it to the schemas"


def test_setup_page_shows_the_exact_first_principal_409_text() -> None:
    """Integration (E2 x E4): the wizard renders the backend's curated 409
    text verbatim, so the two copies of the sentence must stay identical."""
    from pathlib import Path

    from mcprouter.api.routes_policy import FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN

    page = Path(__file__).resolve().parents[1] / "ui/src/pages/setup/SetupPage.tsx"
    m = re.search(r'export const FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN = "([^"]*)";', page.read_text())
    assert m is not None, "SetupPage.tsx no longer exports the constant"
    assert m.group(1) == FIRST_PRINCIPAL_NEEDS_ADMIN_TOKEN
