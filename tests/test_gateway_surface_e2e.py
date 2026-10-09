"""MT-3: the whole MCP surface, both protocol eras, over the REAL transport.

The EXPECTED surface is derived from the server and the installed SDK, never
from a hand list:

* methods: every client request method the SDK defines for an era
  (``mcp_types.methods.CLIENT_REQUESTS`` keyed by that era's protocol version)
  split into HANDLED (``Server.get_request_handler`` returns an entry, or the
  runner-owned ``initialize``) and UNSUPPORTED (must answer -32601), plus any
  non-spec handler registered on the server (custom methods);
* ``tools/call`` per meta-tool the gateway renders (+ one catalog-tool row);
* every ``*/list_changed`` notification the server advertises (handshake era:
  ``create_initialization_options()``; modern era: delivered via
  ``subscriptions/listen``).

``SURFACE`` holds one runnable row per expected key and era; ``UNSUPPORTED``
the -32601 rows. ``test_surface_table_matches_server`` fails when a handler,
meta-tool or notification is added without a row (or a row goes stale);
``test_surface_row`` runs each row through the mcp client against uvicorn and
asserts, via the recording ASGI wrapper, that the request crossed HTTP.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
import pytest
from mcp.client.session import ClientSession
from mcp.client.subscriptions import listen
from mcp.shared.exceptions import MCPError
from mcp_types.methods import CLIENT_REQUESTS
from mcp_types.version import LATEST_HANDSHAKE_VERSION, LATEST_MODERN_VERSION

from mcprouter.gateway.server import (
    _SKILL_TOOL_DEFS,
    ACTIVATE_SKILL_TOOL,
    FEEDBACK_TOOL,
    META_TOOL,
    READ_SKILL_RESOURCE_TOOL,
    GatewayServer,
)
from mcprouter.interfaces import RoutedTool, RouteRequest, RouteResult
from tests.support.execution import Catalog, add_rule
from tests.support.gateway import (
    ERAS,
    HANDSHAKE,
    MODERN,
    UNSUPPORTED_CALLS,
    WireRecorder,
    alive,
    mcp_session,
    notify_stream_ready,
)
from tests.support.gateway import world as world  # noqa: F401 — fixture
from tests.support.serve import run_app
from tests.support.skills_exposure import FakePolicy, _exp, _seed

from .conftest import requires_db

pytestmark = requires_db

ERA_VERSION = {HANDSHAKE: LATEST_HANDSHAKE_VERSION, MODERN: LATEST_MODERN_VERSION}
PROMPT = "local/pdf-tools"
RESOURCE = "skill://local/pdf-tools/guide.md"
CATALOG_TOOL = "github.list_issues"
NOTIFY = "notify:"
CALL = "tools/call:"


# ---------------------------------------------------------------- derivation
def spec_methods(era: str) -> set[str]:
    return {m for m, v in CLIENT_REQUESTS if v == ERA_VERSION[era]}


def handled_methods(gw: GatewayServer, era: str) -> set[str]:
    spec = spec_methods(era)
    handled = {m for m in spec if m == "initialize" or gw.server.get_request_handler(m)}
    # A non-spec (custom) handler is surface too, on both eras. Registered
    # handlers are only enumerable through the Server's own map (mcp 2.3.0).
    all_spec = {m for m, _ in CLIENT_REQUESTS}
    handled |= {m for m in gw.server._request_handlers if m not in all_spec}
    return handled


def unsupported_methods(gw: GatewayServer, era: str) -> set[str]:
    return spec_methods(era) - handled_methods(gw, era)


def meta_tool_names(gw: GatewayServer) -> set[str]:
    names = {t.name for t in gw._render([])}
    if gw._skills is not None:
        names |= {t.name for t in _SKILL_TOOL_DEFS}
    return names


def advertised_notifications(gw: GatewayServer) -> set[str]:
    caps = gw.server.create_initialization_options().capabilities
    out = set()
    for kind, cap in (
        ("tools", caps.tools),
        ("prompts", caps.prompts),
        ("resources", caps.resources),
    ):
        if cap is not None and cap.list_changed:
            out.add(f"notifications/{kind}/list_changed")
    return out


def expected_surface(gw: GatewayServer) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for era in ERAS:
        # tools/call is covered per target (each meta-tool + a catalog tool).
        keys |= {(era, m) for m in handled_methods(gw, era) if m != "tools/call"}
        keys |= {(era, CALL + t) for t in meta_tool_names(gw)}
        keys.add((era, CALL + "catalog"))
        keys |= {(era, NOTIFY + n) for n in advertised_notifications(gw)}
    return keys


def expected_unsupported(gw: GatewayServer) -> set[tuple[str, str]]:
    return {(era, m) for era in ERAS for m in unsupported_methods(gw, era)}


# ---------------------------------------------------------------- fixture
class SurfaceRoute:
    """Routes alice to one catalog tool and one skill."""

    def __init__(self, cat: Catalog, skill_id: str) -> None:
        self.cat, self.skill_id, self.n = cat, skill_id, 0

    def __call__(self, request: RouteRequest) -> RouteResult:
        self.n += 1
        tool = self.cat.tools[CATALOG_TOOL]
        return RouteResult(
            request_id=f"00000000-0000-0000-0000-{self.n:012d}",
            tools=[
                RoutedTool(tool.id, "github", "list_issues", 0.9),
                RoutedTool(self.skill_id, "", "", 0.8, kind="skill"),
            ],
            fallback_used=False,
            latency_ms=1.0,
            model_version="surface",
        )


@dataclass
class Live:
    gw: GatewayServer
    url: str
    wire: WireRecorder
    route: SurfaceRoute
    world: dict[str, Any]

    async def reroute(self, agent: str = "alice") -> None:
        await self.gw.apply_route(agent, self.route(RouteRequest("q", agent, 8)))


@pytest.fixture()
def surface(world: dict[str, Any], tmp_path: Path) -> Iterator[Live]:  # noqa: F811
    db, gw = world["db"], world["gw"]
    skill_a, _ = _seed(db, tmp_path / "src")
    gw._skills = _exp(db, FakePolicy())
    route = SurfaceRoute(world["cat"], skill_a)
    gw._route_fn = route
    add_rule(db, "alice", max_operation="write")
    add_rule(db, "bob", max_operation="write")
    wire = WireRecorder(world["app"])
    port, stop = run_app(wire)
    try:
        yield Live(gw, f"http://127.0.0.1:{port}/mcp", wire, route, world)
    finally:
        stop()


@contextlib.asynccontextmanager
async def opened(
    live: Live, era: str, agent: str = "alice", **kw: Any
) -> AsyncIterator[ClientSession]:
    async with mcp_session(live.url, agent, era, **kw) as session:
        yield session


Row = Callable[[Live, str], Awaitable[None]]


# ---------------------------------------------------------------- rows
async def _initialize(live: Live, era: str) -> None:
    async with opened(live, era) as s:
        await alive(s, era)
    assert any(m == "initialize" for e, m, _ in live.wire.calls if e == era)


async def _discover(live: Live, era: str) -> None:
    async with opened(live, era) as s:
        res = await s.discover()
    assert res.capabilities.tools is not None


async def _ping(live: Live, era: str) -> None:
    async with opened(live, era) as s:
        assert isinstance(await s.send_ping(), types.EmptyResult)


async def _tools_list(live: Live, era: str) -> None:
    async with opened(live, era) as s:
        names = [t.name for t in (await s.list_tools()).tools]
    assert META_TOOL in names and ACTIVATE_SKILL_TOOL in names


async def _prompts_list(live: Live, era: str) -> None:
    await live.reroute()
    async with opened(live, era) as s:
        assert [p.name for p in (await s.list_prompts()).prompts] == [PROMPT]


async def _prompts_get(live: Live, era: str) -> None:
    await live.reroute()
    async with opened(live, era) as s:
        res = await s.get_prompt(PROMPT)
    assert "BODY pdf-tools" in res.messages[0].content.text  # type: ignore[union-attr]


async def _resources_list(live: Live, era: str) -> None:
    await live.reroute()
    async with opened(live, era) as s:
        uris = [str(r.uri) for r in (await s.list_resources()).resources]
    assert uris == [RESOURCE]


async def _resources_read(live: Live, era: str) -> None:
    await live.reroute()
    async with opened(live, era) as s:
        res = await s.read_resource(RESOURCE)
    assert res.contents[0].text == "# Guide"  # type: ignore[union-attr]


async def _templates_list(live: Live, era: str) -> None:
    async with opened(live, era) as s:
        assert (await s.list_resource_templates()).resource_templates == []


async def _listen(live: Live, era: str) -> None:
    async with opened(live, era) as s, listen(s, tools_list_changed=True) as sub:
        await live.reroute()
        with anyio.fail_after(5):
            event = await sub.__anext__()
    assert type(event).__name__ == "ToolsListChanged"


async def _call_catalog(live: Live, era: str) -> None:
    inv = live.world["inv"]
    async with opened(live, era) as s:
        res = await s.call_tool(CATALOG_TOOL, {"repo": "a/b"})
    assert res.is_error is False and inv.calls[-1] == ("github", "list_issues", {"repo": "a/b"})


async def _call_find_tools(live: Live, era: str) -> None:
    async with opened(live, era) as s:
        res = await s.call_tool(META_TOOL, {"query": "list issues"})
    assert res.is_error is False and CATALOG_TOOL in res.content[0].text  # type: ignore[union-attr]


async def _call_feedback(live: Live, era: str) -> None:
    await live.reroute()
    async with opened(live, era) as s:
        res = await s.call_tool(FEEDBACK_TOOL, {"items": [{"name": CATALOG_TOOL, "helpful": True}]})
    text = res.content[0].text  # type: ignore[union-attr]
    # The fake route's decision is not persisted, so the curated "decision
    # not found" proves the call reached record_feedback over the wire.
    assert text.startswith(("Recorded feedback", "Feedback not recorded: decision not found"))


async def _call_activate(live: Live, era: str) -> None:
    await live.reroute()
    async with opened(live, era) as s:
        res = await s.call_tool(ACTIVATE_SKILL_TOOL, {"name": PROMPT})
    assert res.is_error is False and "BODY pdf-tools" in res.content[0].text  # type: ignore[union-attr]


async def _call_read_resource(live: Live, era: str) -> None:
    await live.reroute()
    async with opened(live, era) as s:
        res = await s.call_tool(READ_SKILL_RESOURCE_TOOL, {"name": PROMPT, "path": "guide.md"})
    assert res.is_error is False and res.content[0].text == "# Guide"  # type: ignore[union-attr]


def _notify(kind: str) -> Row:
    """A ``notifications/<kind>/list_changed`` row: delivered on the handshake
    GET stream, or on a modern ``subscriptions/listen`` stream."""
    handshake_type = {
        "tools": types.ToolListChangedNotification,
        "prompts": types.PromptListChangedNotification,
        "resources": types.ResourceListChangedNotification,
    }[kind]
    event_name = {
        "tools": "ToolsListChanged",
        "prompts": "PromptsListChanged",
        "resources": "ResourcesListChanged",
    }[kind]

    async def row(live: Live, era: str) -> None:
        if era == HANDSHAKE:
            got = anyio.Event()

            async def on_message(msg: Any) -> None:
                if isinstance(msg, handshake_type):
                    got.set()

            async with opened(live, era, message_handler=on_message) as s:
                await s.list_tools()  # tracked for notifications
                await notify_stream_ready(live.gw, "alice")
                await live.reroute()
                with anyio.fail_after(5):
                    await got.wait()
            return
        flags: dict[str, Any] = {f"{kind}_list_changed": True}
        async with opened(live, era) as s, listen(s, **flags) as sub:
            await live.reroute()
            with anyio.fail_after(5):
                event = await sub.__anext__()
        assert type(event).__name__ == event_name

    return row


def _unsupported(method: str) -> Row:
    async def row(live: Live, era: str) -> None:
        async with opened(live, era) as s:
            with pytest.raises(MCPError) as info:
                await UNSUPPORTED_CALLS[method](s)
            assert info.value.code == types.METHOD_NOT_FOUND
            await alive(s, era)  # not wedged

    return row


_METHOD_ROWS: dict[str, Row] = {
    "initialize": _initialize,
    "server/discover": _discover,
    "ping": _ping,
    "tools/list": _tools_list,
    "prompts/list": _prompts_list,
    "prompts/get": _prompts_get,
    "resources/list": _resources_list,
    "resources/read": _resources_read,
    "resources/templates/list": _templates_list,
    "subscriptions/listen": _listen,
}
_CALL_ROWS: dict[str, Row] = {
    CALL + "catalog": _call_catalog,
    CALL + META_TOOL: _call_find_tools,
    CALL + FEEDBACK_TOOL: _call_feedback,
    CALL + ACTIVATE_SKILL_TOOL: _call_activate,
    CALL + READ_SKILL_RESOURCE_TOOL: _call_read_resource,
}
_NOTIFY_ROWS: dict[str, Row] = {
    NOTIFY + f"notifications/{k}/list_changed": _notify(k)
    for k in ("tools", "prompts", "resources")
}

# One table per protocol era. A method appears only where the SDK defines it
# for that era (ping/initialize: handshake; server/discover, listen: modern).
SURFACE: dict[tuple[str, str], Row] = {
    **{
        (HANDSHAKE, k): r
        for k, r in _METHOD_ROWS.items()
        if k not in ("server/discover", "subscriptions/listen")
    },
    **{(MODERN, k): r for k, r in _METHOD_ROWS.items() if k not in ("initialize", "ping")},
    **{(era, k): r for era in ERAS for k, r in {**_CALL_ROWS, **_NOTIFY_ROWS}.items()},
}
UNSUPPORTED: dict[tuple[str, str], Row] = {
    (HANDSHAKE, m): _unsupported(m)
    for m in (
        "completion/complete",
        "logging/setLevel",
        "resources/subscribe",
        "resources/unsubscribe",
    )
} | {(MODERN, "completion/complete"): _unsupported("completion/complete")}


def _wire_key(key: str) -> tuple[str, str | None] | None:
    """The (method, tool) a row must have sent over HTTP (None for notify rows,
    whose proof is the client-side delivery the row itself asserts)."""
    if key.startswith(NOTIFY):
        return None
    if key.startswith(CALL):
        tool = key[len(CALL) :]
        return "tools/call", CATALOG_TOOL if tool == "catalog" else tool
    return key, None


# ---------------------------------------------------------------- meta-test
def test_surface_table_matches_server(surface: Live) -> None:
    expected = expected_surface(surface.gw)
    assert not expected - set(SURFACE), f"uncovered MCP surface: {sorted(expected - set(SURFACE))}"
    assert not set(SURFACE) - expected, f"stale SURFACE rows: {sorted(set(SURFACE) - expected)}"
    unsupported = expected_unsupported(surface.gw)
    assert set(UNSUPPORTED) == unsupported, (
        f"unsupported drift: missing {sorted(unsupported - set(UNSUPPORTED))}, "
        f"stale {sorted(set(UNSUPPORTED) - unsupported)}"
    )


def _ids(keys: Any) -> list[str]:
    return [f"{era}|{key}" for era, key in keys]


@pytest.mark.filterwarnings("ignore::mcp.shared.exceptions.MCPDeprecationWarning")
@pytest.mark.parametrize(
    "row", sorted({**SURFACE, **UNSUPPORTED}), ids=_ids(sorted({**SURFACE, **UNSUPPORTED}))
)
async def test_surface_row(surface: Live, row: tuple[str, str]) -> None:
    era, key = row
    await {**SURFACE, **UNSUPPORTED}[row](surface, era)
    wire = _wire_key(key)
    if wire is not None:
        assert (era, *wire) in surface.wire.calls, f"{row} never crossed HTTP: {surface.wire.calls}"


# ---------------------------------------------------------------- extras
@pytest.mark.parametrize("era", ERAS)
async def test_unexposed_but_authorized_tool_is_callable_after_reroute(
    surface: Live, era: str
) -> None:
    """Exposure is not an authz boundary: a tool dropped by the latest route
    stays callable for an agent whose policy allows it."""
    await surface.reroute()  # exposes only github.list_issues
    async with opened(surface, era) as s:
        listed = [t.name for t in (await s.list_tools()).tools]
        assert "github.create_issue" not in listed
        res = await s.call_tool("github.create_issue", {"repo": "a/b"})
    assert res.is_error is False
    assert surface.world["inv"].calls[-1][:2] == ("github", "create_issue")


async def test_two_sessions_of_one_agent_both_notified_other_agent_not(surface: Live) -> None:
    seen: dict[str, int] = {"a1": 0, "a2": 0, "bob": 0}

    def counter(name: str) -> Callable[[Any], Awaitable[None]]:
        async def on_message(msg: Any) -> None:
            if isinstance(msg, types.ToolListChangedNotification):
                seen[name] += 1

        return on_message

    async with (
        opened(surface, HANDSHAKE, message_handler=counter("a1")) as a1,
        opened(surface, HANDSHAKE, message_handler=counter("a2")) as a2,
        opened(surface, HANDSHAKE, agent="bob", message_handler=counter("bob")) as bob,
    ):
        for s in (a1, a2, bob):
            await s.list_tools()
        await notify_stream_ready(surface.gw, "alice")
        await notify_stream_ready(surface.gw, "bob")
        await surface.reroute("alice")
        with anyio.fail_after(5):
            while not (seen["a1"] and seen["a2"]):
                await anyio.sleep(0.01)
        await alive(bob, HANDSHAKE)  # a round-trip after the fan-out
    assert seen["a1"] >= 1 and seen["a2"] >= 1 and seen["bob"] == 0
