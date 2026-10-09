"""P-206: ONE act-as-agent implementation (api/acting.py), table-tested over
every route that uses it."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api import acting, deps_auth
from mcprouter.api.app import create_app
from mcprouter.models import AgentPrincipal
from mcprouter.settings import Settings

from .conftest import requires_db

pytestmark = requires_db

ADMIN = "admin_" + "k" * 40
KEY_A = "agent-a-" + "x" * 32
KEY_B = "agent-b-" + "y" * 32
ROUTES = [
    ("POST", "/api/v1/tools/t0/execute"),
    ("POST", "/api/v1/skills/s0/activate"),
    ("GET", "/api/v1/skills/bundle"),
    ("GET", "/api/v1/skills/s0/resources/a.txt"),
]


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client(db: sessionmaker[Session], settings: Settings) -> Iterator[TestClient]:
    deps_auth._reset_dev_warning_for_tests()
    st = replace(settings, agent_keys=f"agent-a:{KEY_A},agent-off:{KEY_B}")
    c = TestClient(create_app(st, env={"MCPR_ADMIN_TOKEN": ADMIN}))
    with db() as s:
        s.execute(
            update(AgentPrincipal)
            .where(AgentPrincipal.agent_id == "agent-off")
            .values(enabled=False)
        )
        s.commit()
    yield c


def _call(c: TestClient, method: str, path: str, token: str, agent: str | None):  # noqa: ANN202
    if path.endswith("/activate"):  # agentId travels in the body there
        return c.request(method, path, json={"agentId": agent} if agent else {}, headers=_h(token))
    params = {"agentId": agent} if agent else None
    return c.request(method, path, params=params, headers=_h(token))


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_admin_without_agent_id_is_400(client: TestClient, method: str, path: str) -> None:
    r = _call(client, method, path, ADMIN, None)
    assert (r.status_code, r.json()["detail"]) == (400, acting.ADMIN_NEEDS_AGENT)


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_admin_naming_unknown_agent_is_404(client: TestClient, method: str, path: str) -> None:
    r = _call(client, method, path, ADMIN, "ghost")
    assert (r.status_code, r.json()["detail"]) == (404, acting.UNKNOWN_AGENT)


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_admin_naming_disabled_agent_is_403(client: TestClient, method: str, path: str) -> None:
    r = _call(client, method, path, ADMIN, "agent-off")
    assert (r.status_code, r.json()["detail"]) == (403, acting.AGENT_DISABLED)


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_agent_naming_another_agent_is_403(client: TestClient, method: str, path: str) -> None:
    r = _call(client, method, path, KEY_A, "agent-off")
    assert (r.status_code, r.json()["detail"]) == (403, acting.AGENT_ONLY_SELF)


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_bad_agent_key_is_401(client: TestClient, method: str, path: str) -> None:
    r = _call(client, method, path, "not-a-real-key", None)
    assert r.status_code == 401


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_admin_naming_enabled_agent_passes_the_gate(
    client: TestClient, method: str, path: str
) -> None:
    """Gate passes; the route's own outcome (200 execute status / 404 unknown
    skill / bundle of nothing) is not an identity error."""
    r = _call(client, method, path, ADMIN, "agent-a")
    assert r.status_code not in (400, 401, 403), r.text


def test_admin_actor_names_the_audit_actor(client: TestClient) -> None:
    """admin_actor declares require_admin as a sub-dependency (MT-2 sees it)."""
    from mcprouter.api.routes_tools import router

    calls = set()
    for route in router.routes:
        stack = [route.dependant]  # type: ignore[attr-defined]
        while stack:
            d = stack.pop()
            calls.add(d.call)
            stack.extend(d.dependencies)
    assert deps_auth.require_admin in calls and acting.admin_actor in calls
    assert client.get("/api/v1/tools", headers=_h(ADMIN)).status_code == 200
    assert client.get("/api/v1/tools", headers=_h(KEY_A)).status_code == 401


def test_registry_api_deps_facade_keeps_its_names() -> None:
    from mcprouter.api import deps, errors
    from mcprouter.registry import api_deps

    assert api_deps.get_session is deps.get_session
    assert api_deps.require_admin is acting.admin_actor
    assert api_deps.curated_errors is errors.curated_errors
    assert (api_deps.ADMIN_ACTOR, api_deps.DEV_ACTOR) == ("admin", "local-dev")
