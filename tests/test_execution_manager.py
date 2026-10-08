"""Execution manager — the adversarial battery (FR-07; SPEC §10 target: ZERO
unauthorized executions). Every refusal asserts BOTH that the upstream invoker
was never called AND that an audit row was written."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.execution.manager import ApprovalError, ExecutionManager
from mcprouter.execution.models import ApprovalRequest
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.execution.redaction import MASK
from mcprouter.models import AgentPrincipal, ExecutionRecord, MCPToolRecord, PolicyRule

from .conftest import requires_db
from .test_execution_support import (
    STRICT_SCHEMA,
    Catalog,
    FakeInvoker,
    add_rule,
    schema_hash,
    sec_db_fixture,  # noqa: F401 — registers the fixture
    seed,
)

pytestmark = requires_db

OK_ARGS: dict[str, Any] = {"repo": "acme/widgets", "limit": 5}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _mgr(
    factory: sessionmaker[Session],
    invoker: FakeInvoker,
    *,
    limit: int = 1000,
    timeout_s: float = 2.0,
    clock: Clock | None = None,
) -> ExecutionManager:
    return ExecutionManager(
        factory,
        invoker,
        timeout_s=timeout_s,
        limiter=SlidingWindowLimiter(limit),
        clock=clock or Clock(),
    )


def _records(factory: sessionmaker[Session]) -> list[ExecutionRecord]:
    with factory() as s:
        return list(s.scalars(select(ExecutionRecord).order_by(ExecutionRecord.created_at)).all())


def _outcomes(factory: sessionmaker[Session]) -> list[str]:
    return [r.outcome for r in _records(factory)]


@pytest.fixture()
def cat(sec_db: sessionmaker[Session]) -> Catalog:
    return seed(
        sec_db,
        [
            ("github", "list_issues", "read"),
            ("github", "create_issue", "write"),
            ("shell", "run_command", "execute"),
            ("mystery", "frobnicate", "unknown"),
        ],
    )


# ------------------------------------------------------------- authz core
async def test_unauthorized_execution_is_refused_and_audited(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    """No rule at all: the canonical unauthorized call. Mutation target for the
    policy call in ExecutionManager.execute."""
    inv = FakeInvoker()
    res = await _mgr(sec_db, inv).execute(
        cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS
    )
    assert res.status == "denied"
    assert res.detail == "no matching policy rule"
    assert inv.calls == []
    assert _outcomes(sec_db) == ["denied"]


async def test_key_for_agent_a_cannot_use_agent_b_allowed_tool(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "bob", tool_name="*", max_operation="execute")
    inv = FakeInvoker()
    res = await _mgr(sec_db, inv).execute(
        cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS
    )
    assert res.status == "denied" and inv.calls == []


async def test_write_ceiling_agent_cannot_call_execute_tool(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice", max_operation="write")
    inv = FakeInvoker()
    mgr = _mgr(sec_db, inv)
    res = await mgr.execute(cat.principals["alice"], cat.tools["shell.run_command"], OK_ARGS)
    assert res.status == "denied" and "exceeds" in res.detail
    ok = await mgr.execute(cat.principals["alice"], cat.tools["github.create_issue"], OK_ARGS)
    assert ok.status == "ok"
    assert [c[1] for c in inv.calls] == ["create_issue"]


async def test_unknown_operation_tool_under_read_ceiling_is_denied(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice", tool_name="*", max_operation="read")
    inv = FakeInvoker()
    res = await _mgr(sec_db, inv).execute(
        cat.principals["alice"], cat.tools["mystery.frobnicate"], OK_ARGS
    )
    assert res.status == "denied" and inv.calls == []


async def test_stale_caller_tool_record_is_not_trusted(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    """The manager re-loads the tool: a caller holding a stale 'read' record
    cannot run a tool since reclassified as execute (TOCTOU)."""
    add_rule(sec_db, "alice", max_operation="read")
    stale = cat.tools["github.list_issues"]
    with sec_db() as s:
        s.get(MCPToolRecord, stale.id).operation = "execute"  # type: ignore[union-attr]
        s.commit()
    inv = FakeInvoker()
    res = await _mgr(sec_db, inv).execute(cat.principals["alice"], stale, OK_ARGS)
    assert res.status == "denied" and inv.calls == []


async def test_principal_disabled_after_authentication_is_denied(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice", max_operation="execute")
    with sec_db() as s:
        s.scalars(
            select(AgentPrincipal).where(AgentPrincipal.agent_id == "alice")
        ).one().enabled = False
        s.commit()
    inv = FakeInvoker()
    res = await _mgr(sec_db, inv).execute(
        cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS
    )
    assert res.status == "denied" and inv.calls == []


async def test_unknown_tool_id_is_denied_and_audited(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    inv = FakeInvoker()
    res = await _mgr(sec_db, inv).execute(cat.principals["alice"], "no-such-tool-id", OK_ARGS)
    assert res.status == "denied" and inv.calls == []
    assert _outcomes(sec_db) == ["denied"]


async def test_disabled_tool_is_unavailable(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice")
    with sec_db() as s:
        s.get(MCPToolRecord, cat.tools["github.list_issues"].id).enabled = False  # type: ignore[union-attr]
        s.commit()
    inv = FakeInvoker()
    res = await _mgr(sec_db, inv).execute(
        cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS
    )
    assert res.status == "unavailable" and inv.calls == []


# ------------------------------------------------------- argument handling
async def test_argument_smuggling_extra_property_refused(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice")
    inv = FakeInvoker()
    smuggled = {"repo": "acme/w", "sk_live_SMUGGLEDNAME": "hunter2-value"}
    res = await _mgr(sec_db, inv).execute(
        cat.principals["alice"], cat.tools["github.list_issues"], smuggled
    )
    assert res.status == "invalid_args" and inv.calls == []
    assert res.errors == ["$: additionalProperties"]
    rec = _records(sec_db)[-1]
    for leak in ("SMUGGLEDNAME", "hunter2"):
        assert leak not in rec.detail and leak not in res.detail


async def test_validation_errors_never_echo_values(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice")
    inv = FakeInvoker()
    res = await _mgr(sec_db, inv).execute(
        cat.principals["alice"],
        cat.tools["github.list_issues"],
        {"repo": "x", "limit": "ghp_SECRETVALUEnotAnInt"},
    )
    assert res.status == "invalid_args"
    assert res.errors == ["$.limit: type"]
    assert "SECRETVALUE" not in _records(sec_db)[-1].detail


async def test_non_object_arguments_refused(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice")
    inv = FakeInvoker()
    res = await _mgr(sec_db, inv).execute(
        cat.principals["alice"], cat.tools["github.list_issues"], ["repo"]
    )
    assert res.status == "invalid_args" and inv.calls == []


async def test_arguments_cannot_be_mutated_between_validation_and_invoke(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    """The validated arguments are a private deep copy (TOCTOU on args): the
    caller's dict is mutated AFTER validation (inside the pre-invoke audit
    write) and the upstream must still receive what was validated."""
    add_rule(sec_db, "alice")
    inv = FakeInvoker()
    args: dict[str, Any] = {"repo": "acme/widgets", "limit": 5}

    class Racing(ExecutionManager):
        def _audit(self, *a: Any, **kw: Any) -> str:
            if a[3] == "started":
                args["limit"] = "DROP-EVERYTHING"
                args["smuggled"] = "x"
            return super()._audit(*a, **kw)

    mgr = Racing(sec_db, inv, timeout_s=2.0, limiter=SlidingWindowLimiter(10), clock=Clock())
    res = await mgr.execute(cat.principals["alice"], cat.tools["github.list_issues"], args)
    assert res.status == "ok"
    assert inv.calls == [("github", "list_issues", {"repo": "acme/widgets", "limit": 5})]


async def test_audit_failure_prevents_invocation(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    """No unaudited execution: if the pre-invoke audit row cannot be written,
    the upstream is never called."""
    add_rule(sec_db, "alice")
    inv = FakeInvoker()

    class AuditDown(ExecutionManager):
        def _audit(self, *a: Any, **kw: Any) -> str:
            if a[3] == "started":
                raise OSError("audit store unavailable")
            return super()._audit(*a, **kw)

    mgr = AuditDown(sec_db, inv, timeout_s=2.0, limiter=SlidingWindowLimiter(10), clock=Clock())
    with pytest.raises(OSError):
        await mgr.execute(cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS)
    assert inv.calls == []


# ---------------------------------------------------------------- limits
async def test_rate_limit_exhaustion(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice")
    inv = FakeInvoker()
    mgr = _mgr(sec_db, inv, limit=3)
    statuses = [
        (
            await mgr.execute(cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS)
        ).status
        for _ in range(5)
    ]
    assert statuses == ["ok", "ok", "ok", "rate_limited", "rate_limited"]
    assert len(inv.calls) == 3
    assert _outcomes(sec_db).count("rate_limited") == 2


async def test_rate_limit_is_per_agent(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice")
    add_rule(sec_db, "bob")
    inv = FakeInvoker()
    mgr = _mgr(sec_db, inv, limit=1)
    t = cat.tools["github.list_issues"]
    assert (await mgr.execute(cat.principals["alice"], t, OK_ARGS)).status == "ok"
    assert (await mgr.execute(cat.principals["alice"], t, OK_ARGS)).status == "rate_limited"
    assert (await mgr.execute(cat.principals["bob"], t, OK_ARGS)).status == "ok"


async def test_timeout_is_audited(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice")
    inv = FakeInvoker(behavior="hang")
    res = await _mgr(sec_db, inv, timeout_s=0.2).execute(
        cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS
    )
    assert res.status == "timeout"
    assert _outcomes(sec_db) == ["timeout"]


# ------------------------------------------------------ curated upstream errors
async def test_upstream_crash_text_never_reaches_caller_or_audit(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice")
    inv = FakeInvoker(behavior="crash")
    res = await _mgr(sec_db, inv).execute(
        cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS
    )
    assert res.status == "error"
    assert res.detail == "upstream invocation failed"
    assert "SECRETPW" not in _records(sec_db)[-1].detail


async def test_curated_invocation_error_is_redacted(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice")
    inv = FakeInvoker(behavior="curated_error")
    res = await _mgr(sec_db, inv).execute(
        cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS
    )
    assert res.status == "error"
    assert "leaky" not in res.detail and "leaky" not in _records(sec_db)[-1].detail


async def test_every_attempt_is_audited_once(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice", max_operation="write")
    inv = FakeInvoker()
    mgr = _mgr(sec_db, inv)
    p = cat.principals["alice"]
    await mgr.execute(p, cat.tools["github.list_issues"], OK_ARGS)  # ok
    await mgr.execute(p, cat.tools["shell.run_command"], OK_ARGS)  # denied
    await mgr.execute(p, cat.tools["github.list_issues"], {"bad": 1})  # invalid
    recs = _records(sec_db)
    assert sorted(r.outcome for r in recs) == ["denied", "invalid_args", "ok"]
    assert all(r.agent_id == "alice" for r in recs)
    assert {r.tool_id for r in recs} == {
        cat.tools["github.list_issues"].id,
        cat.tools["shell.run_command"].id,
    }


async def test_successful_call_updates_usage_stats(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice")
    mgr = _mgr(sec_db, FakeInvoker())
    for _ in range(2):
        await mgr.execute(cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS)
    with sec_db() as s:
        t = s.get(MCPToolRecord, cat.tools["github.list_issues"].id)
        assert t is not None and t.call_count == 2 and t.avg_latency_ms is not None


# --------------------------------------------------------------- approvals
async def test_requires_approval_bypass_by_calling_twice(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice", requires_approval=True)
    inv = FakeInvoker()
    mgr = _mgr(sec_db, inv)
    p, t = cat.principals["alice"], cat.tools["github.list_issues"]
    r1 = await mgr.execute(p, t, OK_ARGS)
    r2 = await mgr.execute(p, t, OK_ARGS)
    assert r1.status == r2.status == "pending_approval"
    assert r1.approval_id and r2.approval_id and r1.approval_id != r2.approval_id
    assert inv.calls == []
    assert _outcomes(sec_db) == ["pending_approval", "pending_approval"]


async def test_approve_executes_exactly_once(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice", requires_approval=True)
    inv = FakeInvoker()
    mgr = _mgr(sec_db, inv)
    pending = await mgr.execute(cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS)
    assert pending.approval_id
    done = await mgr.approve(pending.approval_id)
    assert done.status == "ok"
    assert inv.calls == [("github", "list_issues", OK_ARGS)]
    with pytest.raises(ApprovalError) as ei:
        await mgr.approve(pending.approval_id)
    assert ei.value.code == "already_decided"
    assert len(inv.calls) == 1
    with sec_db() as s:
        row = s.get(ApprovalRequest, pending.approval_id)
        assert row is not None and row.status == "executed" and row.arguments is None


async def test_concurrent_approvals_execute_once(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice", requires_approval=True)
    inv = FakeInvoker()
    mgr = _mgr(sec_db, inv)
    pending = await mgr.execute(cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS)
    assert pending.approval_id
    outcomes: list[str] = []

    async def one() -> None:
        try:
            outcomes.append((await mgr.approve(pending.approval_id or "")).status)
        except ApprovalError as e:
            outcomes.append(e.code)

    async with anyio.create_task_group() as tg:
        for _ in range(8):
            tg.start_soon(one)
    assert sorted(outcomes).count("ok") == 1
    assert len(inv.calls) == 1


async def test_expired_approval_cannot_execute(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice", requires_approval=True)
    clock = Clock()
    inv = FakeInvoker()
    mgr = _mgr(sec_db, inv, clock=clock)
    pending = await mgr.execute(cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS)
    clock.now += timedelta(minutes=10, seconds=1)
    with pytest.raises(ApprovalError) as ei:
        await mgr.approve(pending.approval_id or "")
    assert ei.value.code == "expired"
    assert inv.calls == []
    with sec_db() as s:
        row = s.get(ApprovalRequest, pending.approval_id)
        assert row is not None and row.status == "expired" and row.arguments is None


async def test_approval_ttl_boundary_still_valid_just_before(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice", requires_approval=True)
    clock = Clock()
    inv = FakeInvoker()
    mgr = _mgr(sec_db, inv, clock=clock)
    pending = await mgr.execute(cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS)
    clock.now += timedelta(minutes=9, seconds=59)
    assert (await mgr.approve(pending.approval_id or "")).status == "ok"


async def test_denied_approval_cannot_execute(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice", requires_approval=True)
    inv = FakeInvoker()
    mgr = _mgr(sec_db, inv)
    pending = await mgr.execute(cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS)
    await mgr.deny(pending.approval_id or "")
    with pytest.raises(ApprovalError):
        await mgr.approve(pending.approval_id or "")
    assert inv.calls == []


async def test_unknown_approval_id(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    with pytest.raises(ApprovalError) as ei:
        await _mgr(sec_db, FakeInvoker()).approve("00000000-0000-0000-0000-000000000000")
    assert ei.value.code == "not_found"


async def test_policy_revoked_before_approval_blocks_execution(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice", requires_approval=True)
    inv = FakeInvoker()
    mgr = _mgr(sec_db, inv)
    pending = await mgr.execute(cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS)
    with sec_db() as s:
        s.execute(delete(PolicyRule))
        s.commit()
    res = await mgr.approve(pending.approval_id or "")
    assert res.status == "denied" and inv.calls == []


async def test_schema_change_voids_pending_approval(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice", requires_approval=True)
    inv = FakeInvoker()
    mgr = _mgr(sec_db, inv)
    pending = await mgr.execute(cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS)
    new_schema = dict(STRICT_SCHEMA, required=["repo", "limit"])
    with sec_db() as s:
        t = s.get(MCPToolRecord, cat.tools["github.list_issues"].id)
        assert t is not None
        t.input_schema = new_schema
        t.schema_hash = schema_hash(new_schema)
        s.commit()
    res = await mgr.approve(pending.approval_id or "")
    assert res.status == "denied" and "schema changed" in res.detail
    assert inv.calls == []


async def test_approval_summary_is_redacted(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    schema = {"type": "object", "properties": {"repo": {}, "token": {}, "note": {}}}
    cat2 = seed(sec_db, [("vault", "write_secret", "write")], agents=("carol",), schema=schema)
    add_rule(sec_db, "carol", max_operation="write", requires_approval=True)
    mgr = _mgr(sec_db, FakeInvoker())
    args = {"repo": "acme/w", "token": "tok-plain", "note": "uses ghp_" + "Z" * 36}
    pending = await mgr.execute(cat2.principals["carol"], cat2.tools["vault.write_secret"], args)
    view = await mgr.get_approval(pending.approval_id or "")
    assert view is not None
    summary = view.summary
    assert summary["tool"] == "vault.write_secret"
    assert summary["operation"] == "write"
    assert summary["arguments"]["repo"] == "acme/w"
    assert summary["arguments"]["token"] == MASK
    assert "ghp_" not in summary["arguments"]["note"]


async def test_agent_can_only_poll_own_approvals(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    add_rule(sec_db, "alice", requires_approval=True)
    mgr = _mgr(sec_db, FakeInvoker())
    pending = await mgr.execute(cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS)
    assert await mgr.get_approval(pending.approval_id or "", agent_id="alice") is not None
    assert await mgr.get_approval(pending.approval_id or "", agent_id="bob") is None


async def test_approval_args_cleared_when_invoke_path_fails(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    """A crash inside approve() must not leave raw arguments at rest."""
    add_rule(sec_db, "alice", requires_approval=True)
    inv = FakeInvoker()

    class AuditDown(ExecutionManager):
        def _audit(self, *a: Any, **kw: Any) -> str:
            if a[3] == "started":
                raise OSError("audit store unavailable")
            return super()._audit(*a, **kw)

    mgr = AuditDown(sec_db, inv, timeout_s=2.0, limiter=SlidingWindowLimiter(10), clock=Clock())
    pending = await mgr.execute(cat.principals["alice"], cat.tools["github.list_issues"], OK_ARGS)
    with pytest.raises(OSError):
        await mgr.approve(pending.approval_id or "")
    assert inv.calls == []
    with sec_db() as s:
        row = s.get(ApprovalRequest, pending.approval_id)
        assert row is not None and row.status == "failed" and row.arguments is None
