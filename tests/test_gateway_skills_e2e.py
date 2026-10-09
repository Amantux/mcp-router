"""Skills over a REAL streamable-http gateway + MCP client session (no handler
shortcuts): routed-only prompts/resources, activation audit linked to the
routing request, text vs blob resources, cross-agent isolation, list_changed
on re-route, and parity between the meta-tools and the native paths."""

from __future__ import annotations

import base64
import hashlib
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from sqlalchemy import select, update

from mcprouter.gateway.server import ACTIVATE_SKILL_TOOL, META_TOOL, READ_SKILL_RESOURCE_TOOL
from mcprouter.interfaces import RoutedTool, RouteRequest, RouteResult
from mcprouter.models import ExecutionRecord, SkillRecord, SkillSourceRecord
from tests.test_execution_support import add_rule
from tests.test_gateway_mcp import _client, sec_db_fixture, world  # noqa: F401 — fixtures
from tests.test_skills_exposure_activation import _exp, _seed

pytestmark = pytest.mark.anyio

PORT = 8803
URL = f"http://127.0.0.1:{PORT}/mcp"
PDF = "local/pdf-tools"
OTHER = "local/other-skill"
FILES: dict[str, bytes] = {
    "guide.md": b"# Guide",
    "page.html": b"<script>alert(1)</script>",  # active content -> blob
    "img.bin": b"\x89PNG\x00\x01\x02",  # binary -> blob
}


class OnlyAlice:
    """Policy seam: allow alice, deny everyone else (audited as denied)."""

    def check(
        self, agent_id: str, skill: SkillRecord, source: SkillSourceRecord
    ) -> tuple[bool, str]:
        return agent_id == "alice", "rule e2e"


class SkillRoute:
    """RouteFn returning the configured skill ids under a fresh request id."""

    def __init__(self) -> None:
        self.picks: list[str] = []
        self.rids: list[str] = []

    def __call__(self, request: RouteRequest) -> RouteResult:
        rid = str(uuid.uuid4())
        self.rids.append(rid)
        tools = [RoutedTool(s, "", "", 1.0, kind="skill") for s in self.picks]
        return RouteResult(rid, tools, False, 1.0, "m")


@pytest.fixture()
def served(world: dict[str, Any], tmp_path: Path) -> Iterator[dict[str, Any]]:  # noqa: F811
    db, gw = world["db"], world["gw"]
    root = tmp_path / "src"
    a, b = _seed(db, root)
    for name, data in FILES.items():
        (root / "pdf-tools" / name).write_bytes(data)
    manifest = [
        {
            "path": n,
            "size": len(d),
            "kind": "text" if n.endswith(".md") else "binary",
            "sha256": hashlib.sha256(d).hexdigest(),
        }
        for n, d in FILES.items()
    ]
    with db() as s:
        s.execute(update(SkillRecord).where(SkillRecord.id == a).values(resource_manifest=manifest))
        s.commit()
    gw._skills = _exp(db, OnlyAlice())  # type: ignore[arg-type]
    route = SkillRoute()
    gw._route_fn = route
    add_rule(db, "alice")
    add_rule(db, "bob")
    server = uvicorn.Server(
        uvicorn.Config(world["app"], host="127.0.0.1", port=PORT, log_level="warning")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    yield {**world, "a": a, "b": b, "route": route}
    server.should_exit = True
    t.join(10)


def _rows(db: Any, agent: str) -> list[ExecutionRecord]:
    with db() as s:
        stmt = select(ExecutionRecord).where(ExecutionRecord.agent_id == agent)
        return list(s.scalars(stmt).all())


def _uri(path: str, skill: str = "pdf-tools") -> str:
    return f"skill://local/{skill}/{path}"


class Changes:
    def __init__(self) -> None:
        self.prompts = anyio.Event()
        self.resources = anyio.Event()

    async def __call__(self, msg: Any) -> None:
        if isinstance(msg, types.PromptListChangedNotification):
            self.prompts.set()
        elif isinstance(msg, types.ResourceListChangedNotification):
            self.resources.set()

    async def wait(self) -> None:
        with anyio.fail_after(5):
            await self.prompts.wait()
            await self.resources.wait()
        self.prompts, self.resources = anyio.Event(), anyio.Event()


async def _reroute(session: ClientSession, route: SkillRoute, picks: list[str]) -> None:
    route.picks = picks
    res = await session.call_tool(META_TOOL, {"query": "skills please"})
    assert res.is_error is False


async def _prompt_names(session: ClientSession) -> list[str]:
    return [p.name for p in (await session.list_prompts()).prompts]


async def test_e2e_skills_routed_audited_isolated(served: dict[str, Any]) -> None:
    db, route, a, b = served["db"], served["route"], served["a"], served["b"]
    changes = Changes()
    async with (
        _client("alice") as http,
        streamable_http_client(URL, http_client=http) as (r, w),
        ClientSession(r, w, message_handler=changes) as session,
    ):
        init = await session.initialize()
        assert init.capabilities.prompts is not None and init.capabilities.prompts.list_changed
        assert init.capabilities.resources is not None
        assert init.capabilities.resources.list_changed
        assert await _prompt_names(session) == []  # skills are opt-in via routing
        await anyio.sleep(0.3)  # let the standalone GET stream attach

        # 1 + 5: routing announces prompts AND resources list_changed; only the
        # routed skill is visible (other-skill exists, enabled, but unrouted).
        await _reroute(session, route, [a])
        await changes.wait()
        assert await _prompt_names(session) == [PDF]

        # 2: prompts/get returns the body; audit row is linked to the route.
        got = await session.get_prompt(PDF)
        assert isinstance(got, types.GetPromptResult)
        body = got.messages[0].content
        assert isinstance(body, types.TextContent) and body.text == "BODY pdf-tools"
        ok = [x for x in _rows(db, "alice") if x.outcome == "ok"]
        assert len(ok) == 1
        assert ok[0].resource_kind == "skill" and ok[0].skill_id == a
        assert ok[0].route_request_id == route.rids[-1]

        # 3: resources/list + read -- text vs blob (active content + binary).
        listed = {str(x.uri): x for x in (await session.list_resources()).resources}
        assert set(listed) == {_uri(n) for n in FILES}
        assert listed[_uri("guide.md")].mime_type == "text/markdown"
        assert listed[_uri("page.html")].mime_type == "application/octet-stream"
        native: dict[str, Any] = {}
        for name, data in FILES.items():
            rr = await session.read_resource(_uri(name))
            assert isinstance(rr, types.ReadResourceResult)
            native[name] = c = rr.contents[0]
            if name.endswith(".md"):
                assert isinstance(c, types.TextResourceContents) and c.text == data.decode()
            else:
                assert isinstance(c, types.BlobResourceContents)
                assert base64.b64decode(c.blob) == data
        reads = [x for x in _rows(db, "alice") if x.outcome == "read"]
        assert len(reads) == len(FILES)
        assert {x.route_request_id for x in reads} == {route.rids[-1]}

        # 6: meta-tools return what the native paths returned.
        tools = {t.name for t in (await session.list_tools()).tools}
        assert {ACTIVATE_SKILL_TOOL, READ_SKILL_RESOURCE_TOOL} <= tools
        act = await session.call_tool(ACTIVATE_SKILL_TOOL, {"name": PDF})
        assert act.is_error is False and isinstance(act.content[0], types.TextContent)
        assert act.content[0].text.startswith(body.text + "\n\n[skill resources] ")
        assert all(f'"path": "{n}"' in act.content[0].text for n in FILES)
        for name in FILES:
            res = await session.call_tool(READ_SKILL_RESOURCE_TOOL, {"name": PDF, "path": name})
            assert res.is_error is False and isinstance(res.content[0], types.TextContent)
            c = native[name]
            want = c.text if name.endswith(".md") else f"[base64 {c.mime_type}] {c.blob}"
            assert res.content[0].text == want

        # 5 again: a re-route to a different skill is announced and swaps the lists.
        await _reroute(session, route, [b])
        await changes.wait()
        assert await _prompt_names(session) == [OTHER]
        uris = {str(x.uri) for x in (await session.list_resources()).resources}
        assert uris == {_uri("guide.md", "other-skill")}
        with pytest.raises(MCPError):
            await session.get_prompt(PDF)  # no longer routed

    # 4: a second agent can neither see nor fetch alice's skill.
    async with (
        _client("bob") as http,
        streamable_http_client(URL, http_client=http) as (r, w),
        ClientSession(r, w) as bob,
    ):
        await bob.initialize()
        assert await _prompt_names(bob) == []
        for call in (bob.get_prompt(OTHER), bob.read_resource(_uri("guide.md", "other-skill"))):
            with pytest.raises(MCPError) as ei:
                await call
            assert ei.value.error.message == "Unknown skill or resource."
        # Unrouted lookups are audited as denied/"not routed" (skill unresolved,
        # so skill_id is None) -- and never "ok"/"read" for bob.
        unrouted = _rows(db, "bob")
        assert sorted((x.outcome, x.detail, x.skill_id, x.resource_kind) for x in unrouted) == [
            ("denied", "not routed", None, "skill"),
            ("denied", "not routed", None, "skill"),
        ]
        # Routed to bob but refused by policy -> curated denial + "denied" audit.
        await _reroute(bob, route, [b])
        with pytest.raises(MCPError) as ei:
            await bob.get_prompt(OTHER)
        assert ei.value.error.message == "Skill access denied by policy."
        denied = [x for x in _rows(db, "bob") if x.skill_id is not None]
        assert [(x.outcome, x.resource_kind, x.skill_id) for x in denied] == [
            ("denied", "skill", b)
        ]
        assert denied[0].route_request_id == route.rids[-1]
        with pytest.raises(MCPError):
            await bob.read_resource(_uri("guide.md", "other-skill"))
        assert {x.outcome for x in _rows(db, "bob")} == {"denied"}
