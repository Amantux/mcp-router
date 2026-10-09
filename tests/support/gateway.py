"""Shared helpers moved out of tests/test_gateway_mcp.py (W0-2); not a test module."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from mcp.server.context import ServerRequestContext
from mcp.shared._httpx_utils import create_mcp_http_client
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.deps_auth import configure_security
from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.gateway.server import (
    PRINCIPAL_SCOPE_KEY,
    GatewayServer,
    build_gateway,
)
from mcprouter.interfaces import RoutedTool, RouteRequest, RouteResult
from mcprouter.settings import Settings
from tests.conftest import TEST_DB_URL
from tests.support.execution import (
    KEYS,
    Catalog,
    FakeInvoker,
    seed,
)


class FakeRoute:
    """In-fence RouteFn: returns a fixed selection (may include tools the agent
    is NOT authorized for — the gateway must filter them)."""

    def __init__(self, cat: Catalog, picks: list[str]) -> None:
        self.cat = cat
        self.picks = picks
        self.requests: list[RouteRequest] = []
        self.fail = False

    def __call__(self, request: RouteRequest) -> RouteResult:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("laya exploded: token=leakme")
        return RouteResult(
            request_id=str(uuid.uuid4()),
            tools=[
                RoutedTool(self.cat.tools[p].id, p.split(".")[0], p.split(".", 1)[1], 0.9)
                for p in self.picks
            ],
            fallback_used=False,
            latency_ms=1.0,
            model_version="fake",
        )


TOOLS = [
    ("github", "list_issues", "read"),
    ("github", "create_issue", "write"),
    ("github", "search_code", "read"),
    ("shell", "run_command", "execute"),
    ("files", "read_file", "read"),
    ("mystery", "frobnicate", "unknown"),
]


COUNTS = {"files.read_file": 50, "github.search_code": 40, "github.list_issues": 10}


@pytest.fixture()
def world(sec_db: sessionmaker[Session]) -> Iterator[dict[str, Any]]:
    cat = seed(sec_db, TOOLS, call_counts=COUNTS)
    settings = Settings(database_url=TEST_DB_URL, max_exposed_tools=8)
    app = FastAPI()
    app.state.settings = settings
    app.state.engine = sec_db.kw["bind"]
    app.state.session_factory = sec_db
    configure_security(app, {"MCPR_ADMIN_TOKEN": "admin_" + "z" * 30})
    inv = FakeInvoker()
    mgr = ExecutionManager(sec_db, inv, timeout_s=2.0, limiter=SlidingWindowLimiter(1000))
    route = FakeRoute(cat, ["github.list_issues", "shell.run_command", "github.create_issue"])
    gw = build_gateway(app, manager=mgr, route_fn=route)
    yield {"cat": cat, "app": app, "gw": gw, "inv": inv, "route": route, "db": sec_db}


def _ctx(gw: GatewayServer, cat: Catalog, agent: str, version: str = "2025-11-25") -> Any:
    class Req:
        def __init__(self) -> None:
            self.scope = {
                PRINCIPAL_SCOPE_KEY: cat.principals[agent],
                "type": "http",
                "headers": [],
            }

    return ServerRequestContext(
        session=None,  # type: ignore[arg-type]
        lifespan_context={},
        protocol_version=version,
        method="tools/list",
        request=Req(),
    )


async def _names(gw: GatewayServer, cat: Catalog, agent: str) -> list[str]:
    res = await gw._on_list_tools(_ctx(gw, cat, agent), None)
    return [t.name for t in res.tools]


def _client(agent: str | None) -> Any:
    headers = {"Authorization": f"Bearer {KEYS[agent]}"} if agent else {}
    return create_mcp_http_client(headers=headers)
