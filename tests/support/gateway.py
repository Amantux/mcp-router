"""Shared helpers moved out of tests/test_gateway_mcp.py (W0-2); not a test module."""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

import anyio
import mcp_types as types
import pytest
from fastapi import FastAPI
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.context import ServerRequestContext
from mcp.server.streamable_http import GET_STREAM_KEY
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import Headers

from mcprouter.api.deps_auth import configure_security
from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.gateway.server import (
    MCP_PATH,
    PRINCIPAL_SCOPE_KEY,
    GatewayServer,
    build_gateway,
)
from mcprouter.interfaces import RoutedTool, RouteRequest, RouteResult
from mcprouter.limits import make_limiters
from mcprouter.settings import Settings
from tests.conftest import TEST_DB_URL
from tests.support.execution import (
    KEYS,
    Catalog,
    FakeInvoker,
    seed,
)
from tests.support.serve import run_app


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
    app.state.limiters = make_limiters(settings)
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


HANDSHAKE, MODERN = "handshake", "modern"
ERAS = (HANDSHAKE, MODERN)


class WireRecorder:
    """Outermost, test-only ASGI wrapper around the served app.

    Records, for every JSON-RPC POST that reaches ``/mcp`` over real HTTP, the
    ``(era, method, tool-or-None)`` triple (era from the ``mcp-protocol-version``
    header: a modern version -> modern, absent/handshake -> handshake), and
    every response body chunk, so a test can prove a call crossed the wire and
    that nothing secret was written to it. The triple is recorded as soon as
    the request body is complete (before the response), so it is visible by
    the time the client sees the answer."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.calls: list[tuple[str, str, str | None]] = []
        self.statuses: list[tuple[str, int]] = []
        self.bodies: list[bytes] = []

    @staticmethod
    def era_of(headers: Headers) -> str:
        version = headers.get("mcp-protocol-version")
        return MODERN if version in MODERN_PROTOCOL_VERSIONS else HANDSHAKE

    def wire_text(self) -> bytes:
        return b"".join(self.bodies)

    def _record(self, era: str, raw: bytes) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if isinstance(msg, dict) and isinstance(msg.get("method"), str):
            got = msg.get("params")
            params: dict[str, Any] = got if isinstance(got, dict) else {}
            name = params.get("name") if msg["method"] == "tools/call" else None
            self.calls.append((era, msg["method"], name if isinstance(name, str) else None))

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") != MCP_PATH:
            await self.app(scope, receive, send)
            return
        era = self.era_of(Headers(scope=scope))
        chunks: list[bytes] = []

        async def rcv() -> Any:
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body") and scope["method"] == "POST":
                    self._record(era, b"".join(chunks))
            return msg

        async def snd(msg: Any) -> None:
            if msg["type"] == "http.response.start":
                self.statuses.append((scope["method"], msg["status"]))
            elif msg["type"] == "http.response.body":
                self.bodies.append(msg.get("body", b""))
            await send(msg)

        await self.app(scope, rcv, snd)


@pytest.fixture()
def live(world: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """``world`` served over real HTTP on an OS-assigned port, wrapped in a
    :class:`WireRecorder` (``live["wire"]``); ``live["url"]`` is the /mcp URL."""
    wire = WireRecorder(world["app"])
    port, stop = run_app(wire)
    try:
        yield {**world, "url": f"http://127.0.0.1:{port}{MCP_PATH}", "wire": wire}
    finally:
        stop()


@contextlib.asynccontextmanager
async def mcp_session(
    url: str, agent: str, era: str, **session_kw: Any
) -> AsyncIterator[ClientSession]:
    """An opened client session of ``agent`` in ``era`` (initialize / discover)."""
    async with (
        _client(agent) as http,
        streamable_http_client(url, http_client=http) as (r, w),
        ClientSession(r, w, **session_kw) as session,
    ):
        if era == HANDSHAKE:
            await session.initialize()
        else:
            await session.discover()
        yield session


async def alive(session: ClientSession, era: str) -> None:
    """Prove the server still answers this session. ``ping`` is a
    handshake-era method only (the 2026-07-28 era answers it -32601, verified
    against mcp 2.3.0), so the modern era re-runs ``server/discover``."""
    if era == HANDSHAKE:
        await session.send_ping()
    else:
        await session.discover()


def get_stream_attached(gw: GatewayServer, sid: str) -> bool:
    """Readiness probe replacing a fixed sleep: the handshake session ``sid``
    has its standalone GET (server->client notification) stream attached.
    Reads SDK internals (mcp 2.3.0 ``StreamableHTTPSessionManager``)."""
    transport = gw.server.session_manager._server_instances.get(sid)
    return transport is not None and GET_STREAM_KEY in transport._request_streams


async def notify_stream_ready(gw: GatewayServer, agent: str, timeout: float = 5.0) -> None:
    """Replaces the old fixed ``sleep(0.3)``: wait (without blocking the
    loop the client runs on) until every tracked handshake session of
    ``agent`` has its standalone GET notification stream attached."""
    with anyio.fail_after(timeout):
        while True:
            sids = list(gw._legacy.get(agent, {}))
            if sids and all(get_stream_attached(gw, sid) for sid in sids):
                return
            await anyio.sleep(0.01)


# Spec methods the gateway deliberately does NOT handle (SDK answers -32601),
# each driven through the real client. Shared by the P-308 pin and MT-3.
UNSUPPORTED_CALLS: dict[str, Callable[[ClientSession], Awaitable[Any]]] = {
    "completion/complete": lambda s: s.complete(
        types.PromptReference(type="ref/prompt", name="x"), {"name": "a", "value": "b"}
    ),
    "logging/setLevel": lambda s: s.set_logging_level("info"),
    "resources/subscribe": lambda s: s.subscribe_resource("skill://a/b/c"),
    "resources/unsubscribe": lambda s: s.unsubscribe_resource("skill://a/b/c"),
}
