"""Shared fixtures/helpers for the gateway workstream's tests (no tests here).

Lives under a test_execution* name to stay inside the workstream's file fence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import anyio
import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.deps_auth import hash_key
from mcprouter.execution.models import ApprovalRequest, init_security_db
from mcprouter.interfaces import ToolCallResult, ToolInvocationError
from mcprouter.models import AgentPrincipal, MCPServerRecord, MCPToolRecord, PolicyRule

KEYS = {"alice": "key_alice_" + "a" * 30, "bob": "key_bob_" + "b" * 30}

STRICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"repo": {"type": "string"}, "limit": {"type": "integer"}},
    "required": ["repo"],
    "additionalProperties": False,
}


@pytest.fixture(name="sec_db")
def sec_db_fixture(db: sessionmaker[Session]) -> Iterator[sessionmaker[Session]]:
    engine = db.kw["bind"]
    init_security_db(engine)
    yield db
    with db() as s:
        s.execute(delete(ApprovalRequest))
        s.commit()


def schema_hash(schema: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()


@dataclass
class Catalog:
    servers: dict[str, MCPServerRecord] = field(default_factory=dict)
    tools: dict[str, MCPToolRecord] = field(default_factory=dict)  # key: "server.tool"
    principals: dict[str, AgentPrincipal] = field(default_factory=dict)


def seed(
    factory: sessionmaker[Session],
    tools: list[tuple[str, str, str]],  # (server, tool, operation)
    *,
    agents: tuple[str, ...] = ("alice", "bob"),
    schema: dict[str, Any] | None = None,
    call_counts: dict[str, int] | None = None,
) -> Catalog:
    cat = Catalog()
    sch = schema if schema is not None else STRICT_SCHEMA
    with factory() as s:
        for agent in agents:
            p = AgentPrincipal(
                agent_id=agent, key_hash=hash_key(KEYS.get(agent, agent + "_k" * 10))
            )
            s.add(p)
            cat.principals[agent] = p
        for server_name, tool_name, op in tools:
            srv = cat.servers.get(server_name)
            if srv is None:
                srv = MCPServerRecord(name=server_name, transport="stdio", enabled=True)
                s.add(srv)
                s.flush()
                cat.servers[server_name] = srv
            stable = f"{server_name}.{tool_name}"
            t = MCPToolRecord(
                server_id=srv.id,
                name=tool_name,
                description=f"{tool_name} on {server_name}",
                input_schema=sch,
                schema_hash=schema_hash(sch),
                operation=op,
                call_count=(call_counts or {}).get(stable, 0),
            )
            s.add(t)
            cat.tools[stable] = t
        s.commit()
    return cat


def add_rule(
    factory: sessionmaker[Session],
    agent_id: str,
    *,
    server_id: str | None = None,
    tool_name: str | None = None,
    max_operation: str = "read",
    requires_approval: bool = False,
) -> PolicyRule:
    with factory() as s:
        r = PolicyRule(
            agent_id=agent_id,
            server_id=server_id,
            tool_name=tool_name,
            max_operation=max_operation,
            requires_approval=requires_approval,
        )
        s.add(r)
        s.commit()
        return r


@dataclass
class FakeInvoker:
    """In-fence ToolInvoker fake. Records every upstream call."""

    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    behavior: str = "ok"  # ok | is_error | curated_error | crash | hang
    text: str = "done"

    async def call_tool(
        self, server: Any, tool_name: str, arguments: dict[str, Any], timeout_s: float
    ) -> ToolCallResult:
        self.calls.append((server.name, tool_name, dict(arguments)))
        if self.behavior == "hang":
            await anyio.sleep(3600)
        if self.behavior == "curated_error":
            raise ToolInvocationError("upstream returned 502 password=leaky")
        if self.behavior == "crash":
            raise RuntimeError("psycopg: connection to postgresql://u:SECRETPW@db failed")
        return ToolCallResult(
            content=[{"type": "text", "text": self.text}], is_error=self.behavior == "is_error"
        )
