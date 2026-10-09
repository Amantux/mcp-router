"""END-TO-END skills story (wave 4): one real uvicorn + testbed fleet + a
generated skills corpus, no fakes on the request path.

  admin registers a directory skill source -> sync -> post-sync hook classifies
  (scripted => execute) and embeds -> read-ceiling skill rule for agent1 ->
  agent1 POST /route with a mixed query returns tools AND skills -> MCP
  prompts/list shows exactly the routed skills; prompts/get is audited and
  attributed to the route -> no execute-class skill ever reaches the
  read-ceiling agent -> the bundle zip carries every routed skill -> analytics
  counts the activation, and an admin /route/simulate leaves it unchanged.
"""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from testbed.harness import http_fleet
from testbed.skills.generate import generate

from mcprouter.api.app import create_app
from mcprouter.models import ExecutionRecord, SkillRecord
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db

pytestmark = requires_db

APP_PORT = 8820
FLEET_PORT = 8821
BASE = f"http://127.0.0.1:{APP_PORT}"
ADMIN = "e2e-skills-admin-" + "a" * 24
AGENT_KEY = "e2e-skills-agent1-" + "b" * 24
ADMIN_H = {"Authorization": f"Bearer {ADMIN}"}
AGENT_H = {"Authorization": f"Bearer {AGENT_KEY}"}
SOURCE = "corpus"
MIXED = "search issues and records in git"
QUERIES = [
    MIXED,
    "run the ci pipeline and lint the python code",
    "extract text from a pdf document",
    "send an email newsletter invite",
    "query the postgres schema and redis cache",
    "export a meeting report to csv",
]


@pytest.fixture()
def stack(db: sessionmaker[Session], tmp_path: Path) -> Iterator[dict[str, Any]]:
    corpus = tmp_path / "skills"
    truth = generate(corpus, 60, seed=7, include_invalid=False)
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
            yield {"urls": urls, "db": db, "corpus": corpus, "truth": truth}
        finally:
            server.should_exit = True
            t.join(15)


async def test_end_to_end_skills_register_sync_route_expose_bundle_analytics(
    stack: dict[str, Any],
) -> None:
    db: sessionmaker[Session] = stack["db"]
    truth: dict[str, dict[str, Any]] = stack["truth"]
    assert len(truth) >= 30 and any(v["has_scripts"] for v in truth.values())
    scripted = {n for n, v in truth.items() if v["has_scripts"]}

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as http:
        # MCP side: register + refresh the testbed fleet; agent1 reads github.
        ids: dict[str, str] = {}
        for name, url in sorted(stack["urls"].items()):
            r = await http.post(
                "/api/v1/servers",
                json={"name": name, "transport": "streamable-http", "endpoint": url},
                headers=ADMIN_H,
            )
            assert r.status_code == 201, r.text
            ids[name] = r.json()["id"]
            r = await http.post(f"/api/v1/servers/{ids[name]}/refresh", headers=ADMIN_H)
            assert r.status_code == 200, r.text
        r = await http.post(
            "/api/v1/policy-rules",
            json={"agentId": "agent1", "serverId": ids["github"], "maxOperation": "read"},
            headers=ADMIN_H,
        )
        assert r.status_code == 201, r.text

        # Skills side: register the directory source and sync it.
        src = {"name": SOURCE, "kind": "directory", "location": str(stack["corpus"])}
        r = await http.post("/api/v1/skill-sources", json=src, headers=ADMIN_H)
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        r = await http.post(f"/api/v1/skill-sources/{sid}/sync", headers=ADMIN_H)
        assert r.status_code == 200, r.text

        with db() as s:
            recs = {k.name: k for k in s.scalars(select(SkillRecord))}
            s.expunge_all()
        assert set(recs) == set(truth), sorted(set(recs) ^ set(truth))
        known = [k for k in recs.values() if k.operation != "unknown"]
        assert len(known) >= 0.8 * len(recs), [(k.name, k.operation) for k in recs.values()]
        assert all(recs[n].operation == "execute" for n in scripted)
        assert all(k.embedding_backend for k in recs.values())
        execute = {n for n, k in recs.items() if k.operation == "execute"}

        r = await http.post(
            "/api/v1/policy-rules",
            json={
                "agentId": "agent1",
                "serverId": sid,
                "resourceKind": "skill",
                "toolName": "*",
                "maxOperation": "read",
            },
            headers=ADMIN_H,
        )
        assert r.status_code == 201, r.text

        # Read-ceiling invariant across several queries (ground truth + DB class).
        surfaced: set[str] = set()
        for q in QUERIES[1:]:
            r = await http.post("/api/v1/route", json={"query": q}, headers=AGENT_H)
            assert r.status_code == 200, r.text
            got = {k["skill"] for k in r.json()["skills"]}
            assert not got & (execute | scripted), (q, got & (execute | scripted))
            surfaced |= got
        assert surfaced, "no skill surfaced across the probe queries"

        # The mixed query LAST, so it is agent1's live route for the MCP session.
        r = await http.post("/api/v1/route", json={"query": MIXED}, headers=AGENT_H)
        assert r.status_code == 200, r.text
        routed = r.json()
        rid = routed["request_id"]
        assert routed["tools"] and routed["skills"], routed
        assert "max_skills_applied" in routed or "maxSkillsApplied" in routed, routed
        names = sorted(k["skill"] for k in routed["skills"])
        assert not set(names) & (execute | scripted)
        assert all("search" in n or "records" in n for n in names[:1]), names
        pick = names[0]

        ov0 = (await http.get("/api/v1/analytics/overview", headers=ADMIN_H)).json()

    async with (
        create_mcp_http_client(headers=AGENT_H) as mcp_http,
        streamable_http_client(f"{BASE}/mcp", http_client=mcp_http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        prompts = sorted(p.name for p in (await session.list_prompts()).prompts)
        assert prompts == sorted(f"{SOURCE}/{n}" for n in names), prompts
        prompt = await session.get_prompt(f"{SOURCE}/{pick}")
        text = " ".join(getattr(m.content, "text", "") for m in prompt.messages)
        assert pick in text, text

    with db() as s:
        rows = list(
            s.scalars(
                select(ExecutionRecord).where(
                    ExecutionRecord.agent_id == "agent1",
                    ExecutionRecord.resource_kind == "skill",
                    ExecutionRecord.skill_id == recs[pick].id,
                )
            )
        )
    assert rows and all(x.route_request_id == rid for x in rows), [
        (x.outcome, x.route_request_id) for x in rows
    ]

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as http:
        r = await http.get("/api/v1/skills/bundle", headers=AGENT_H)
        assert r.status_code == 200, r.text
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        members = set(zf.namelist())
        assert ".mcp-router-bundle.json" in members, members
        assert {f"{n}/SKILL.md" for n in names} <= members, members
        json.loads(zf.read(".mcp-router-bundle.json"))

        ov = (await http.get("/api/v1/analytics/overview", headers=ADMIN_H)).json()
        assert ov["skills"]["activated"] >= 1, ov.get("skills")
        assert ov["contextEconomy"]["skillMetadataTokens"] > 0, ov.get("contextEconomy")
        assert ov0["skills"] != ov["skills"] or ov0["skills"]["activated"] >= 1

        r = await http.post(
            "/api/v1/route/simulate",
            json={"query": MIXED, "agentId": "agent1"},
            headers=ADMIN_H,
        )
        assert r.status_code == 200, r.text
        ov2 = (await http.get("/api/v1/analytics/overview", headers=ADMIN_H)).json()
        assert ov2["skills"] == ov["skills"], (ov["skills"], ov2["skills"])
