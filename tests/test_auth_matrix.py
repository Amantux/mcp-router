"""MT-2 (wave-6 P-210): the auth matrix is GENERATED from the live route
table, not hand-listed. See tests/support/auth_matrix.py for the classes.

* every OpenAPI route is classified (ADMIN by introspection; AGENT / EITHER /
  PUBLIC by explicit table) — an unclassified route is red;
* ADMIN: no bearer, an agent key and a wrong token are 401; the admin passes;
* AGENT / EITHER: no bearer and a wrong token are 401; the agent key passes;
  EITHER also lets the admin through the auth layer.

Failures are aggregated per class so one run lists every offending row.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api import deps_auth
from mcprouter.api.app import create_app
from mcprouter.settings import Settings
from tests.support.auth_matrix import (
    ADMIN,
    AGENT,
    AGENT_ROUTES,
    EITHER,
    EITHER_ROUTES,
    PUBLIC_ROUTES,
    RouteInfo,
    api_routes,
    classify,
)

from .conftest import requires_db

pytestmark = requires_db

ADMIN_TOKEN = "admin_" + "m" * 40
AGENT_KEY = "agent-m-" + "w" * 32
WRONG = "wrong-" + "v" * 34
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"
# Bodies that pass schema validation, for routes that authenticate INSIDE the
# handler (body validation would otherwise answer 422 before auth runs).
VALID_BODY: dict[tuple[str, str], object] = {
    ("POST", "/api/v1/route/{request_id}/feedback"): {"items": [{"id": "x", "helpful": True}]},
    ("POST", "/api/v1/route"): {"query": "x"},
}


@pytest.fixture()
def app(db: sessionmaker[Session], settings: Settings) -> Iterator[FastAPI]:
    deps_auth._reset_dev_warning_for_tests()
    st = replace(settings, agent_keys=f"matrix-agent:{AGENT_KEY}")
    yield create_app(st, env={"MCPR_ADMIN_TOKEN": ADMIN_TOKEN})


def _url(path: str) -> str:
    return re.sub(r"\{[^}]+\}", PLACEHOLDER, path)


def _call(c: TestClient, r: RouteInfo, token: str | None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = VALID_BODY.get((r.method, r.path), {})
    kwargs: dict[str, Any] = {"json": body} if r.method in {"POST", "PUT", "PATCH"} else {}
    return c.request(r.method, _url(r.path), headers=headers, **kwargs).status_code


def test_every_route_is_classified(app: FastAPI) -> None:
    routes = api_routes(app)
    keys = {(r.method, r.path) for r in routes}
    unclassified = sorted(f"{r.method} {r.path}" for r in routes if classify(r) is None)
    assert not unclassified, f"unclassified routes (add to auth_matrix tables): {unclassified}"
    stale = sorted(
        f"{m} {p}"
        for m, p in {**AGENT_ROUTES, **EITHER_ROUTES, **PUBLIC_ROUTES}
        if (m, p) not in keys
    )
    assert not stale, f"auth_matrix lists routes that no longer exist: {stale}"


def test_admin_routes_reject_none_agent_and_wrong(app: FastAPI) -> None:
    c = TestClient(app)
    rows = [r for r in api_routes(app) if classify(r) == ADMIN]
    assert rows  # the unclassified test names any route that lost its gate
    bad = []
    for r in rows:
        got = {
            who: _call(c, r, tok)
            for who, tok in (("none", None), ("agent", AGENT_KEY), ("wrong", WRONG))
        }
        if any(code != 401 for code in got.values()):
            bad.append(f"{r.method} {r.path} {got}")
        admin = _call(c, r, ADMIN_TOKEN)
        if admin in (401, 403):
            bad.append(f"{r.method} {r.path} admin={admin}")
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("cls", [AGENT, EITHER])
def test_agent_and_either_routes_require_a_credential(app: FastAPI, cls: str) -> None:
    c = TestClient(app)
    rows = [r for r in api_routes(app) if classify(r) == cls]
    assert rows
    bad = []
    for r in rows:
        got = {who: _call(c, r, tok) for who, tok in (("none", None), ("wrong", WRONG))}
        if any(code != 401 for code in got.values()):
            bad.append(f"{r.method} {r.path} {got}")
        agent = _call(c, r, AGENT_KEY)
        if agent in (401, 403):
            bad.append(f"{r.method} {r.path} agent={agent}")
        if cls == EITHER and _call(c, r, ADMIN_TOKEN) == 401:
            bad.append(f"{r.method} {r.path} admin=401")
    assert not bad, "\n".join(bad)


def test_public_routes_are_open(app: FastAPI) -> None:
    c = TestClient(app)
    for r in api_routes(app):
        if (r.method, r.path) in PUBLIC_ROUTES:
            assert _call(c, r, None) == 200, r.path
