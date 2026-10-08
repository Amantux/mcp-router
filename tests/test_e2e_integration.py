"""END-TO-END acceptance test for the integrated v0.1 tree (definition of done).

One real process stack, no fakes on the request path:

  testbed fleet (3 real MCP servers over streamable HTTP, port 8700)
  <- discovery (admin REST: register + refresh)
  -> post-sync hook on refresh: rule classifier + embed_pending_tools (hash)
  -> admin policy rule: agent1 may READ on server 1 (github) only
  -> agent1 POST /api/v1/route  (authenticated, policy-scoped, hash/deterministic)
  -> agent1 MCP session on /mcp (uvicorn, port 8710): tools/list shows exactly
     the routed, authorized subset; tools/call reaches the real upstream via
     ExecutionManager -> ConnectorToolInvoker -> Connector; audit row written
  -> a server-2 tool and a server-1 write tool are refused AND audited
  -> management API without the admin token is refused.

Wave 2 (integration) extends the story with the efficiency loop: the MCP
tools/call is attributed to agent1's live route (the gateway seam), the
analytics overview/tools show the selection, REST execute works as the agent
(attributed) and as an admin impersonating it (audited, never attributed,
never wider), a /route/simulate is recorded but invisible to analytics, and a
route-cache hit counts as real traffic.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from testbed.harness import http_fleet

from mcprouter.api.app import create_app
from mcprouter.gateway.server import META_TOOL
from mcprouter.inference.hash_backend import HashEmbeddingBackend
from mcprouter.inference.pipeline import embed_pending_tools
from mcprouter.models import ExecutionRecord, MCPToolRecord
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db

pytestmark = requires_db

FLEET_PORT = 8700
APP_PORT = 8710
BASE = f"http://127.0.0.1:{APP_PORT}"
MCP_URL = f"{BASE}/mcp"
ADMIN = "e2e-admin-token-" + "a" * 24
AGENT_KEY = "e2e-agent1-key-" + "b" * 24
ADMIN_H = {"Authorization": f"Bearer {ADMIN}"}
AGENT_H = {"Authorization": f"Bearer {AGENT_KEY}"}


@pytest.fixture()
def stack(db: sessionmaker[Session]) -> Iterator[dict[str, Any]]:
    with http_fleet(3, port_base=FLEET_PORT) as urls:
        app = create_app(
            Settings(database_url=TEST_DB_URL, agent_keys=f"agent1:{AGENT_KEY}"),
            env={"MCPR_ADMIN_TOKEN": ADMIN},
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=APP_PORT, log_level="warning")
        )
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        deadline = time.time() + 20
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"
        try:
            yield {"urls": urls, "db": db, "app": app}
        finally:
            server.should_exit = True
            t.join(15)


def _audit(db: sessionmaker[Session], tool_id: str) -> list[str]:
    with db() as s:
        return list(
            s.scalars(
                select(ExecutionRecord.outcome).where(
                    ExecutionRecord.agent_id == "agent1", ExecutionRecord.tool_id == tool_id
                )
            )
        )


def _tool_id(db: sessionmaker[Session], server_id: str, name: str) -> str:
    with db() as s:
        return s.scalars(
            select(MCPToolRecord.id).where(
                MCPToolRecord.server_id == server_id, MCPToolRecord.name == name
            )
        ).one()


async def test_end_to_end_register_discover_route_expose_execute_audit(
    stack: dict[str, Any],
) -> None:
    urls: dict[str, str] = stack["urls"]
    db: sessionmaker[Session] = stack["db"]
    assert len(urls) >= 3

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        # -- management API refuses anyone without the admin token -------------
        evil = {"name": "evil", "transport": "stdio", "command": ["/bin/sh", "-c", "id"]}
        assert (await http.post("/api/v1/servers", json=evil)).status_code == 401
        assert (await http.post("/api/v1/servers", json=evil, headers=AGENT_H)).status_code == 401

        # -- register + discover 3 real streamable-HTTP servers ----------------
        ids: dict[str, str] = {}
        for name, url in sorted(urls.items()):
            r = await http.post(
                "/api/v1/servers",
                json={"name": name, "transport": "streamable-http", "endpoint": url},
                headers=ADMIN_H,
            )
            assert r.status_code == 201, r.text
            ids[name] = r.json()["id"]
            r = await http.post(f"/api/v1/servers/{ids[name]}/refresh", headers=ADMIN_H)
            assert r.status_code == 200, r.text
            assert r.json()["added"], f"no tools discovered on {name}"
        server1, server2 = "github", "jenkins"
        assert {server1, server2} <= set(ids)

        # -- classify (rules-v1) + embed (hash backend) ------------------------
        # Done by the refresh's post-sync hook (wave 2, integration gap 1):
        # nothing is left for a manual pass.
        with db() as s:
            report = embed_pending_tools(s, HashEmbeddingBackend())
            s.commit()
            unclassified = s.scalar(
                select(func.count())
                .select_from(MCPToolRecord)
                .where(MCPToolRecord.classification_source.is_(None))
            )
        assert report.embedded == 0 and report.skipped > 0
        assert unclassified == 0

        # -- policy: agent1 may READ on server1 only ---------------------------
        r = await http.post(
            "/api/v1/policy-rules",
            json={"agentId": "agent1", "serverId": ids[server1], "maxOperation": "read"},
            headers=ADMIN_H,
        )
        assert r.status_code == 201, r.text

        # -- route as the authenticated agent ----------------------------------
        r = await http.post(
            "/api/v1/route",
            json={"query": "search issues about the login bug", "maxTools": 5},
            headers=AGENT_H,
        )
        assert r.status_code == 200, r.text
        routed = r.json()
        assert routed["tools"], routed
        assert {t["server"] for t in routed["tools"]} == {server1}, routed
        with db() as s:
            ops = set(
                s.scalars(
                    select(MCPToolRecord.operation).where(
                        MCPToolRecord.server_id == ids[server1],
                        MCPToolRecord.name.in_([t["tool"] for t in routed["tools"]]),
                    )
                )
            )
        assert ops == {"read"}, ops
        assert "search_issues" in {t["tool"] for t in routed["tools"]}
        # body naming another agent is spoofing
        spoof = await http.post(
            "/api/v1/route", json={"query": "x", "agentId": "someone-else"}, headers=AGENT_H
        )
        assert spoof.status_code == 403

    # -- MCP gateway: exposure = routed authorized subset; execute end-to-end --
    async with (
        create_mcp_http_client(headers=AGENT_H) as mcp_http,
        streamable_http_client(MCP_URL, http_client=mcp_http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        listed = [t.name for t in (await session.list_tools()).tools]
        exposed = {n for n in listed if n != META_TOOL}
        assert exposed == {f"{server1}.{t['tool']}" for t in routed["tools"]}, listed
        assert all(n.startswith(f"{server1}.") for n in exposed)

        ok = await session.call_tool(f"{server1}.search_issues", {"query": "login bug"})
        assert ok.is_error is False, ok
        payload = json.loads(ok.content[0].text)  # type: ignore[union-attr]
        assert payload["server"] == server1 and payload["tool"] == "search_issues"
        assert payload["args"] == {"query": "login bug"}

        denied = await session.call_tool(f"{server2}.list_pipelines", {"project": "web"})
        assert denied.is_error is True
        assert "denied" in denied.content[0].text  # type: ignore[union-attr]

        write = await session.call_tool(
            f"{server1}.create_issue", {"repo": "a/b", "title": "should not happen"}
        )
        assert write.is_error is True

    # -- audit trail: every attempt recorded, allowed or refused ---------------
    assert _audit(db, _tool_id(db, ids[server1], "search_issues")) == ["ok"]
    assert _audit(db, _tool_id(db, ids[server2], "list_pipelines")) == ["denied"]
    assert _audit(db, _tool_id(db, ids[server1], "create_issue")) == ["denied"]
    with db() as s:
        tool = s.get(MCPToolRecord, _tool_id(db, ids[server1], "search_issues"))
        assert tool is not None and tool.call_count == 1

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        r = await http.get("/api/v1/executions", params={"agentId": "agent1"}, headers=ADMIN_H)
        assert r.status_code == 200
        outcomes = sorted(i["outcome"] for i in r.json()["items"])
        assert outcomes == ["denied", "denied", "ok"], outcomes
        assert (await http.get("/api/v1/executions", headers=AGENT_H)).status_code == 401

    # =================== wave 2: the efficiency loop lights up ===================
    # The gateway passed the agent's live route request_id on tools/call (the
    # cross-branch seam, feature-detected in GatewayServer) — verified on the
    # real audit row, not assumed.
    rid = routed["request_id"]
    search_id = _tool_id(db, ids[server1], "search_issues")
    with db() as s:
        attributed = s.scalars(
            select(ExecutionRecord.route_request_id).where(
                ExecutionRecord.agent_id == "agent1", ExecutionRecord.tool_id == search_id
            )
        ).all()
    assert attributed == [rid], attributed

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        overview = (await http.get("/api/v1/analytics/overview", headers=ADMIN_H)).json()
        assert overview["executions"]["attributionCoverage"] > 0, overview["executions"]
        assert overview["funnel"]["surfaced"] >= len(routed["tools"])
        assert overview["funnel"]["selected"] >= 1 and overview["funnel"]["succeeded"] >= 1
        assert overview["routing"]["decisions"] >= 1
        tools = (await http.get("/api/v1/analytics/tools", headers=ADMIN_H)).json()["items"]
        row = next(t for t in tools if t["toolId"] == search_id)
        assert row["selected"] >= 1 and row["surfaced"] >= 1 and row["toolName"] == "search_issues"

        # -- REST execute (playground) as agent1, attributed -------------------
        r = await http.post(
            f"/api/v1/tools/{search_id}/execute",
            json={"arguments": {"query": "login bug"}, "routeRequestId": rid},
            headers=AGENT_H,
        )
        assert r.status_code == 200, r.text
        rest = r.json()
        assert rest["status"] == "ok" and rest["result"]["isError"] is False, rest
        assert json.loads(rest["result"]["content"][0]["text"])["tool"] == "search_issues"
        assert isinstance(rest["latencyMs"], float)

        # -- the same endpoint, admin impersonating agent1 ---------------------
        assert (
            await http.post(
                f"/api/v1/tools/{search_id}/execute",
                json={"arguments": {"query": "login bug"}},
                headers=ADMIN_H,
            )
        ).status_code == 400
        r = await http.post(
            f"/api/v1/tools/{search_id}/execute",
            params={"agentId": "agent1"},
            json={"arguments": {"query": "login bug"}},
            headers=ADMIN_H,
        )
        assert r.status_code == 200 and r.json()["status"] == "ok", r.text
        imp = r.json()
        # ... and impersonation never widens: a server-2 tool stays denied.
        r = await http.post(
            f"/api/v1/tools/{_tool_id(db, ids[server2], 'list_pipelines')}/execute",
            params={"agentId": "agent1"},
            json={"arguments": {"project": "web"}},
            headers=ADMIN_H,
        )
        assert r.status_code == 200 and r.json()["status"] == "denied", r.text

        execs = (
            await http.get("/api/v1/executions", params={"agentId": "agent1"}, headers=ADMIN_H)
        ).json()["items"]
        by_id = {e["id"]: e for e in execs}
        assert by_id[rest["recordId"]]["routeRequestId"] == rid  # attributed
        assert by_id[imp["recordId"]]["routeRequestId"] is None  # never attributed
        assert by_id[imp["recordId"]]["detail"].startswith(
            "[admin-initiated via REST, impersonating 'agent1']"
        )

        # -- a simulation is recorded but INVISIBLE to analytics ---------------
        before = (await http.get("/api/v1/analytics/overview", headers=ADMIN_H)).json()
        agents_before = (await http.get("/api/v1/analytics/agents", headers=ADMIN_H)).json()
        r = await http.post(
            "/api/v1/route/simulate",
            json={"agentId": "agent1", "query": "search issues about the login bug"},
            headers=ADMIN_H,
        )
        assert r.status_code == 200, r.text
        sim = r.json()
        assert sim["simulated"] is True and sim["tools"]
        with db() as s:
            from mcprouter.models import RoutingDecisionRecord

            sim_row = s.get(RoutingDecisionRecord, sim["requestId"])
            assert sim_row is not None and sim_row.model_version.startswith("simulated/")
        after = (await http.get("/api/v1/analytics/overview", headers=ADMIN_H)).json()
        agents_after = (await http.get("/api/v1/analytics/agents", headers=ADMIN_H)).json()
        for key in ("funnel", "routing", "contextEconomy", "positionCurve"):
            assert after[key] == before[key], key
        assert agents_after["items"] == agents_before["items"]

        # -- a cache hit IS real traffic ---------------------------------------
        r = await http.post(
            "/api/v1/route",
            json={"query": "search issues about the login bug", "maxTools": 5},
            headers=AGENT_H,
        )
        assert r.status_code == 200 and r.json()["cached"] is True, r.text
        cached = (await http.get("/api/v1/analytics/overview", headers=ADMIN_H)).json()
        assert cached["routing"]["decisions"] == before["routing"]["decisions"] + 1
        assert cached["funnel"]["surfaced"] == before["funnel"]["surfaced"] + len(r.json()["tools"])
