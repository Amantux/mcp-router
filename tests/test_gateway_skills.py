"""Gateway skill exposure: prompts/resources handlers and meta-tools all funnel
through the ONE SkillExposure instance (stubbed here to prove the single path)."""

from __future__ import annotations

import base64
from typing import Any

import mcp_types as types
import pytest
from mcp.shared.exceptions import MCPError

from mcprouter.gateway.server import ACTIVATE_SKILL_TOOL, READ_SKILL_RESOURCE_TOOL
from mcprouter.gateway.skills import Activation, SkillAccessError
from mcprouter.interfaces import RoutedTool, RouteResult
from mcprouter.skills.serve import ResourceContent
from tests.support.gateway import _ctx, world  # noqa: F401 — fixtures
from tests.support.gateway_skills import (
    _route,
)

pytestmark = pytest.mark.anyio


class StubSkill:
    def __init__(self, sid: str, name: str) -> None:
        self.id, self.name, self.description = sid, name, "does pdf things"
        self.resource_manifest = [
            {"path": "ref.md", "size": 5, "kind": "text"},
            {"path": "img.png", "size": 3, "kind": "binary"},
            {"bogus": 1},
        ]


class StubSrc:
    name = "local"


class StubExposure:
    """Records every call; only routed ids are visible (like the real one)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def load_routed(self, ids: Any) -> list[tuple[Any, Any]]:
        return [(StubSkill(i, "pdf"), StubSrc()) for i in ids if i == "sk1"]

    def _visible(self, name: str, ids: Any) -> None:
        if "sk1" not in tuple(ids) or name not in ("sk1", "local/pdf"):
            raise SkillAccessError("not_found", "Unknown skill /secret/path sk1")

    def activate(self, agent: str, name: str, ids: Any, rid: Any = None) -> Activation:
        self.calls.append(("activate", agent, name, tuple(ids), rid))
        self._visible(name, ids)
        return Activation("sk1", "local/pdf", "BODY", [{"path": "ref.md"}], "rec1")

    def read_resource(
        self, agent: str, name: str, path: str, ids: Any, rid: Any = None
    ) -> ResourceContent:
        self.calls.append(("read", agent, name, path, tuple(ids), rid))
        self._visible(name, ids)
        if path == "img.png":
            return ResourceContent(path, "image/png", blob=b"\x89PN")
        if path != "ref.md":
            raise SkillAccessError("invalid_path", "bad path ../../etc/passwd")
        return ResourceContent(path, "text/plain", text="hello")


@pytest.fixture()
def gw(world: dict[str, Any]) -> Any:  # noqa: F811
    g = world["gw"]
    g._skills = StubExposure()
    return g


async def test_prompts_and_resources_only_for_routed_agent(gw: Any, world: Any) -> None:  # noqa: F811
    cat = world["cat"]
    await gw.apply_route("alice", _route("rr-1", ["sk1"]))
    res = await gw._on_list_prompts(_ctx(gw, cat, "alice"), None)
    assert [p.name for p in res.prompts] == ["local/pdf"]
    assert (await gw._on_list_prompts(_ctx(gw, cat, "bob"), None)).prompts == []
    rl = await gw._on_list_resources(_ctx(gw, cat, "alice"), None)
    assert [r.uri for r in rl.resources] == [
        "skill://local/pdf/ref.md",
        "skill://local/pdf/img.png",
    ]
    # list MIME comes from the SAME function serve.py uses for reads
    assert [r.mime_type for r in rl.resources] == ["text/markdown", "application/octet-stream"]
    assert (await gw._on_list_resources(_ctx(gw, cat, "bob"), None)).resources == []


async def test_get_prompt_and_meta_tool_share_the_single_path(gw: Any, world: Any) -> None:  # noqa: F811
    cat = world["cat"]
    await gw.apply_route("alice", _route("rr-1", ["sk1"]))
    got = await gw._on_get_prompt(
        _ctx(gw, cat, "alice"), types.GetPromptRequestParams(name="local/pdf")
    )
    assert got.messages[0].content.text == "BODY"
    tool = await gw._on_call_tool(
        _ctx(gw, cat, "alice"),
        types.CallToolRequestParams(name=ACTIVATE_SKILL_TOOL, arguments={"name": "local/pdf"}),
    )
    assert not tool.is_error and "BODY" in tool.content[0].text
    # Both hit the SAME SkillExposure.activate with the agent's routed ids + route id.
    assert gw._skills.calls == [("activate", "alice", "local/pdf", ("sk1",), "rr-1")] * 2


async def test_read_resource_text_blob_and_meta_tool(gw: Any, world: Any) -> None:  # noqa: F811
    cat = world["cat"]
    await gw.apply_route("alice", _route("rr-1", ["sk1"]))
    ctx = _ctx(gw, cat, "alice")
    t = await gw._on_read_resource(
        ctx, types.ReadResourceRequestParams(uri="skill://local/pdf/ref.md")
    )
    assert t.contents[0].text == "hello"
    b = await gw._on_read_resource(
        ctx, types.ReadResourceRequestParams(uri="skill://local/pdf/img.png")
    )
    assert base64.b64decode(b.contents[0].blob) == b"\x89PN"
    m = await gw._on_call_tool(
        ctx,
        types.CallToolRequestParams(
            name=READ_SKILL_RESOURCE_TOOL, arguments={"name": "local/pdf", "path": "ref.md"}
        ),
    )
    assert m.content[0].text == "hello"
    assert [c[0] for c in gw._skills.calls] == ["read", "read", "read"]


@pytest.mark.parametrize(
    "uri", ["skill://local/pdf/../../etc/passwd", "skill://local/other/x", "file:///etc/passwd"]
)
async def test_errors_are_curated(gw: Any, world: Any, uri: str) -> None:  # noqa: F811
    cat = world["cat"]
    await gw.apply_route("alice", _route("rr-1", ["sk1"]))
    with pytest.raises(MCPError) as ei:
        await gw._on_read_resource(_ctx(gw, cat, "alice"), types.ReadResourceRequestParams(uri=uri))
    msg = ei.value.error.message
    assert msg == "Unknown skill or resource." and "passwd" not in msg and "sk1" not in msg


async def test_other_agent_cannot_activate(gw: Any, world: Any) -> None:  # noqa: F811
    cat = world["cat"]
    await gw.apply_route("alice", _route("rr-1", ["sk1"]))
    with pytest.raises(MCPError):
        await gw._on_get_prompt(
            _ctx(gw, cat, "bob"), types.GetPromptRequestParams(name="local/pdf")
        )
    assert gw._skills.calls[-1][1:4] == ("bob", "local/pdf", ())


async def test_reroute_publishes_prompt_and_resource_changes_per_agent(gw: Any) -> None:
    seen: dict[str, list[str]] = {"alice": [], "bob": []}
    for agent in seen:
        bus, _ = gw._bus(agent)
        bus.subscribe(lambda e, a=agent: seen[a].append(type(e).__name__))
    await gw.apply_route("alice", _route("rr-1", ["sk1"]))
    assert seen == {
        "alice": ["ToolsListChanged", "PromptsListChanged", "ResourcesListChanged"],
        "bob": [],
    }
    await gw.apply_route("alice", _route("rr-2", ["sk1"]))  # unchanged set: no notify
    assert len(seen["alice"]) == 3


async def test_apply_route_splits_kinds_between_stores(gw: Any, monkeypatch: Any) -> None:
    seen: list[list[str]] = []
    real_set = gw.exposure.set

    def spy(agent: str, ids: list[str], rid: Any) -> Any:
        seen.append(list(ids))
        return real_set(agent, ids, rid)

    monkeypatch.setattr(gw.exposure, "set", spy)
    tools = [
        RoutedTool("t1", "", "", 1.0, kind="tool"),
        RoutedTool("sk1", "", "", 1.0, kind="skill"),
        RoutedTool("x1", "", "", 1.0, kind="prompt"),  # unknown kind: neither store
    ]
    await gw.apply_route("alice", RouteResult("rr-k", tools, False, 1.0, "m"))
    assert seen == [["t1"]]
    assert gw._skill_ids["alice"] == ("sk1",)
