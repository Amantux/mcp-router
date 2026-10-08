"""Shared analytics fixtures (no tests here): a small, hand-countable world.

Decisions (agent, minutes-ago, surfaced in rank order):
  d1 alice  -60  [A, B]   A called ok twice (counts once), C called (off-funnel)
  d2 alice -120  [B, A]   B error; A has a stray 'started' row (ignored)
  d3 bob   -180  []       no match, fallback
  d4 alice -300  [A, C]   C denied
plus one LEGACY unattributed ok call of A (route_request_id NULL).

Expected funnel: A 3/1/1/0 rank-sum 4; B 2/1/0/1 rank-sum 3; C 1/1/0/0 rank-sum 2.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from mcprouter.models import ExecutionRecord, RoutingDecisionRecord

from .test_execution_support import Catalog, add_rule, seed

NOW = datetime.now(UTC).replace(microsecond=0)


@dataclass
class World:
    cat: Catalog
    A: str
    B: str
    C: str
    D: str  # never surfaced (mystery server)
    d1: str
    d2: str
    d3: str
    d4: str


def add_decision(
    factory: sessionmaker[Session],
    agent: str,
    at: datetime,
    tools: list[str],
    *,
    fallback: bool = False,
    latency_ms: float = 10.0,
    model_version: str = "test",
) -> str:
    rid = str(uuid.uuid4())
    with factory() as s:
        s.add(
            RoutingDecisionRecord(
                id=rid,
                agent_id=agent,
                query="q",
                selected_tool_ids=tools,
                scores={t: 1.0 - i / 10 for i, t in enumerate(tools)},
                model_version=model_version,
                fallback_used=fallback,
                latency_ms=latency_ms,
                created_at=at,
            )
        )
        s.commit()
    return rid


def add_exec(
    factory: sessionmaker[Session],
    agent: str,
    tool_id: str,
    outcome: str,
    rrid: str | None,
    at: datetime,
) -> None:
    with factory() as s:
        s.add(
            ExecutionRecord(
                agent_id=agent,
                tool_id=tool_id,
                outcome=outcome,
                detail="",
                latency_ms=1.0,
                created_at=at,
                route_request_id=rrid,
            )
        )
        s.commit()


def build_world(factory: sessionmaker[Session]) -> World:
    cat = seed(
        factory,
        [
            ("github", "list_issues", "read"),
            ("github", "create_issue", "write"),
            ("shell", "run", "execute"),
            ("mystery", "frob", "read"),
        ],
    )
    A = cat.tools["github.list_issues"].id
    B = cat.tools["github.create_issue"].id
    C = cat.tools["shell.run"].id
    D = cat.tools["mystery.frob"].id
    add_rule(factory, "alice", max_operation="execute")  # any server, any tool
    m = timedelta(minutes=1)
    d1 = add_decision(factory, "alice", NOW - 60 * m, [A, B], latency_ms=10.0)
    d2 = add_decision(factory, "alice", NOW - 120 * m, [B, A], latency_ms=20.0)
    d3 = add_decision(factory, "bob", NOW - 180 * m, [], fallback=True, latency_ms=30.0)
    d4 = add_decision(factory, "alice", NOW - 300 * m, [A, C], latency_ms=40.0)
    add_exec(factory, "alice", A, "ok", d1, NOW - 59 * m)
    add_exec(factory, "alice", A, "ok", d1, NOW - 58 * m)
    add_exec(factory, "alice", C, "ok", d1, NOW - 57 * m)  # off-funnel
    add_exec(factory, "alice", B, "error", d2, NOW - 119 * m)
    add_exec(factory, "alice", A, "started", d2, NOW - 118 * m)
    add_exec(factory, "alice", C, "denied", d4, NOW - 299 * m)
    add_exec(factory, "alice", A, "ok", None, NOW - 10 * m)  # legacy, unattributed
    return World(cat, A, B, C, D, d1, d2, d3, d4)
