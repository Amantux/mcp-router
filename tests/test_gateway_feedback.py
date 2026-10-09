"""router.feedback meta-tool over the real gateway harness: listed beside
router.find_tools (outside the max_tools cap), dispatches to the single
record_feedback implementation, defaults requestId to the agent's last route,
and curates errors as isError results."""

from __future__ import annotations

from typing import Any

import pytest
from mcp import types

from mcprouter.analytics import feedback as fb
from mcprouter.gateway.server import FEEDBACK_TOOL, META_TOOL
from tests.support.analytics import NOW, add_decision
from tests.support.gateway import _ctx, _names, world  # noqa: F401 — fixture

from .conftest import requires_db

pytestmark = requires_db


async def _call(w: dict[str, Any], agent: str, args: dict[str, Any]) -> types.CallToolResult:
    p = types.CallToolRequestParams(name=FEEDBACK_TOOL, arguments=args)
    return await w["gw"]._on_call_tool(_ctx(w["gw"], w["cat"], agent), p)


def _txt(r: types.CallToolResult) -> str:
    return r.content[0].text  # type: ignore[union-attr]


async def test_listed_with_meta_tool(world: dict[str, Any]) -> None:  # noqa: F811
    names = await _names(world["gw"], world["cat"], "alice")
    assert names[-2:] == [META_TOOL, FEEDBACK_TOOL]


async def test_defaults_to_last_route_and_records(world: dict[str, Any]) -> None:  # noqa: F811
    tid = world["cat"].tools["github.list_issues"].id
    rid = add_decision(world["db"], "alice", NOW, [tid])
    world["gw"].exposure.set("alice", [], rid)
    r = await _call(world, "alice", {"items": [{"name": "github.list_issues", "helpful": True}]})
    assert r.is_error is False and "1 item" in _txt(r)


async def test_dispatches_to_record_feedback(
    world: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def stub(session: Any, **kw: Any) -> int:
        seen.update(kw)
        return 7

    monkeypatch.setattr(fb, "record_feedback", stub)
    r = await _call(world, "alice", {"requestId": "r1", "items": [{"name": "x", "helpful": False}]})
    assert "7 item" in _txt(r)
    assert seen["request_id"] == "r1" and seen["agent_id"] == "alice"
    assert seen["source"] == "agent" and seen["items"][0].name == "x"


async def test_other_agents_decision_is_curated_error(world: dict[str, Any]) -> None:  # noqa: F811
    tid = world["cat"].tools["github.list_issues"].id
    rid = add_decision(world["db"], "bob", NOW, [tid])
    r = await _call(
        world, "alice", {"requestId": rid, "items": [{"name": "github.list_issues", "helpful": 1}]}
    )
    assert r.is_error is True  # non-bool helpful rejected
    r = await _call(
        world,
        "alice",
        {"requestId": rid, "items": [{"name": "github.list_issues", "helpful": True}]},
    )
    assert r.is_error is True and _txt(r) == "Feedback not recorded: decision not found"


async def test_no_route_yet_is_curated_error(world: dict[str, Any]) -> None:  # noqa: F811
    r = await _call(world, "alice", {"items": [{"name": "x", "helpful": True}]})
    assert r.is_error is True and "requestId" in _txt(r)
