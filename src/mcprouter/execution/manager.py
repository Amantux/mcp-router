"""Execution manager — the ONLY path from an agent to an upstream tool (FR-07).

Order for every attempt (each refusal is audited and returns a curated reason):

  0. re-load principal, tool, server and rules from the DB (never trust the
     caller's possibly-stale records: TOCTOU between routing and execution)
  a. policy.evaluate            -> denied
  b. per-agent rate limit       -> rate_limited
  c. tool/server availability   -> unavailable
  d. JSON-Schema validation     -> invalid_args   (field paths only, no values)
  e. approval gate              -> pending_approval (ApprovalRequest row)
  f. invoke via ToolInvoker under a hard timeout -> ok | error | timeout

Audit: an ExecutionRecord is written for EVERY attempt. On the invoke path a
`started` row is written BEFORE the upstream call; if that write fails the call
does not happen (no unaudited execution). The row is finalized afterwards,
including on cancellation.

Approvals are single-use: `approve()` claims the row with one conditional
UPDATE (`status='pending' AND expires_at > now`), so concurrent approvals race
on the database and exactly one wins. The claimed call re-runs policy and
validation against CURRENT state, so revoking a rule or changing the tool's
schema after the request voids it.
"""

from __future__ import annotations

import copy
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import anyio
from sqlalchemy import case, select, update
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api.deps_auth import DEV_AGENT_ID
from mcprouter.execution.models import (
    APPROVAL_DENIED,
    APPROVAL_EXECUTED,
    APPROVAL_EXECUTING,
    APPROVAL_EXPIRED,
    APPROVAL_FAILED,
    APPROVAL_PENDING,
    ApprovalRequest,
)
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.execution.redaction import redact, redact_value, scrub_log
from mcprouter.execution.validation import ArgumentValidationError, validate_arguments
from mcprouter.interfaces import ToolCallResult, ToolInvocationError, ToolInvoker
from mcprouter.models import (
    AgentPrincipal,
    ExecutionRecord,
    MCPServerRecord,
    MCPToolRecord,
    PolicyRule,
    utcnow,
)
from mcprouter.policy.engine import effective_operation, evaluate
from mcprouter.settings import Settings

log = logging.getLogger(__name__)

APPROVAL_TTL = timedelta(minutes=10)
MAX_DETAIL = 1000
MAX_PREVIEW = 2000

# ExecutionRecord.outcome values written by this module (String(16)).
OK, ERROR, TIMEOUT, DENIED = "ok", "error", "timeout", "denied"
RATE_LIMITED, INVALID_ARGS, UNAVAILABLE = "rate_limited", "invalid_args", "unavailable"
PENDING_APPROVAL, STARTED, CANCELLED = "pending_approval", "started", "cancelled"


def stable_tool_id(server_name: str, tool_name: str) -> str:
    return f"{server_name}.{tool_name}"


def _curate(text: str) -> str:
    return scrub_log(text)[:MAX_DETAIL]


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    detail: str
    record_id: str | None = None
    result: ToolCallResult | None = None
    approval_id: str | None = None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ApprovalView:
    """What may leave the process about an approval (never raw arguments)."""

    id: str
    agent_id: str
    tool_id: str
    status: str
    summary: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    result_preview: str | None


class ApprovalError(Exception):
    """Curated approval failure. `code`: not_found | expired | already_decided."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _Loaded:
    principal: AgentPrincipal
    tool: MCPToolRecord
    server: MCPServerRecord
    rules: list[PolicyRule]


class ExecutionManager:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        invoker: ToolInvoker,
        *,
        timeout_s: float,
        limiter: SlidingWindowLimiter,
        clock: Callable[[], datetime] = utcnow,
        approval_ttl: timedelta = APPROVAL_TTL,
    ) -> None:
        self._factory = session_factory
        self._invoker = invoker
        self._timeout_s = timeout_s
        self._limiter = limiter
        self._clock = clock
        self._approval_ttl = approval_ttl

    @classmethod
    def from_settings(
        cls, settings: Settings, session_factory: sessionmaker[Session], invoker: ToolInvoker
    ) -> ExecutionManager:
        return cls(
            session_factory,
            invoker,
            timeout_s=settings.default_tool_timeout_s,
            limiter=SlidingWindowLimiter(settings.rate_limit_per_agent_per_min, 60.0),
        )

    # ----------------------------------------------------------- loading
    def _load(self, principal: AgentPrincipal, tool_id: str) -> _Loaded | None:
        with self._factory() as s:
            fresh = s.scalars(
                select(AgentPrincipal).where(AgentPrincipal.agent_id == principal.agent_id)
            ).one_or_none()
            if fresh is None:
                # Only the synthetic dev principal legitimately has no row.
                if principal.agent_id != DEV_AGENT_ID or principal.key_hash != "":
                    return None
                fresh = principal
            tool = s.get(MCPToolRecord, tool_id)
            if tool is None:
                return None
            server = s.get(MCPServerRecord, tool.server_id)
            if server is None:
                return None
            rules = list(
                s.scalars(select(PolicyRule).where(PolicyRule.agent_id == fresh.agent_id)).all()
            )
            for obj in (tool, server, *rules):
                s.expunge(obj)
            if fresh is not principal:
                s.expunge(fresh)
            return _Loaded(fresh, tool, server, rules)

    # ------------------------------------------------------------- audit
    def _audit(
        self,
        agent_id: str,
        tool_id: str | None,
        server_id: str | None,
        outcome: str,
        detail: str,
        latency_ms: float | None = None,
    ) -> str:
        with self._factory() as s:
            rec = ExecutionRecord(
                agent_id=agent_id,
                tool_id=tool_id,
                server_id=server_id,
                outcome=outcome,
                detail=_curate(detail),
                latency_ms=latency_ms,
                created_at=self._clock(),
            )
            s.add(rec)
            s.commit()
            return rec.id

    def _finalize(
        self, record_id: str, tool_id: str, outcome: str, detail: str, latency_ms: float
    ) -> None:
        with self._factory() as s:
            s.execute(
                update(ExecutionRecord)
                .where(ExecutionRecord.id == record_id)
                .values(outcome=outcome, detail=_curate(detail), latency_ms=latency_ms)
            )
            if outcome in (OK, ERROR, TIMEOUT):
                # One atomic UPDATE (RHS reads pre-update values): running mean.
                avg = MCPToolRecord.avg_latency_ms
                s.execute(
                    update(MCPToolRecord)
                    .where(MCPToolRecord.id == tool_id)
                    .values(
                        call_count=MCPToolRecord.call_count + 1,
                        error_count=MCPToolRecord.error_count + (0 if outcome == OK else 1),
                        avg_latency_ms=case(
                            (avg.is_(None), latency_ms),
                            else_=avg + (latency_ms - avg) / (MCPToolRecord.call_count + 1),
                        ),
                    )
                )
            s.commit()

    def _refuse(self, agent_id: str, loaded: _Loaded | None, outcome: str, detail: str) -> str:
        return self._audit(
            agent_id,
            loaded.tool.id if loaded else None,
            loaded.server.id if loaded else None,
            outcome,
            detail,
        )

    # ----------------------------------------------------------- execute
    async def execute(
        self, principal: AgentPrincipal, tool: MCPToolRecord | str, arguments: Any
    ) -> ExecutionResult:
        tool_id = tool if isinstance(tool, str) else tool.id
        # Private deep copy FIRST: what is validated is exactly what is invoked.
        args = copy.deepcopy(arguments)
        agent_id = principal.agent_id

        loaded = await anyio.to_thread.run_sync(self._load, principal, tool_id)
        if loaded is None:
            rid = await anyio.to_thread.run_sync(
                self._audit, agent_id, None, None, DENIED, "unknown tool or principal"
            )
            return ExecutionResult(DENIED, "unknown tool or principal", rid)

        # (a) policy — deterministic, score-free, deny by default.
        decision = evaluate(loaded.principal, loaded.server, loaded.tool, loaded.rules)
        if not decision.allow:
            rid = await anyio.to_thread.run_sync(
                self._refuse, agent_id, loaded, DENIED, decision.reason
            )
            return ExecutionResult(DENIED, decision.reason, rid)

        # (b) rate limit
        if not self._limiter.try_acquire(agent_id):
            detail = "rate limit exceeded"
            rid = await anyio.to_thread.run_sync(
                self._refuse, agent_id, loaded, RATE_LIMITED, detail
            )
            return ExecutionResult(RATE_LIMITED, detail, rid)

        # (c) availability
        if not (loaded.tool.enabled and loaded.tool.available and loaded.server.enabled):
            detail = "tool unavailable"
            rid = await anyio.to_thread.run_sync(
                self._refuse, agent_id, loaded, UNAVAILABLE, detail
            )
            return ExecutionResult(UNAVAILABLE, detail, rid)

        # (d) argument validation — curated paths only
        try:
            args = validate_arguments(loaded.tool.input_schema, args)
        except ArgumentValidationError as exc:
            detail = "argument validation failed: " + "; ".join(exc.errors)
            rid = await anyio.to_thread.run_sync(
                self._refuse, agent_id, loaded, INVALID_ARGS, detail
            )
            return ExecutionResult(INVALID_ARGS, detail, rid, errors=exc.errors)

        # (e) approval gate
        if decision.requires_approval:
            approval_id, rid = await anyio.to_thread.run_sync(self._create_approval, loaded, args)
            return ExecutionResult(
                PENDING_APPROVAL, "approval required", rid, approval_id=approval_id
            )

        # (f) invoke
        return await self._invoke(agent_id, loaded, args)

    async def _invoke(
        self, agent_id: str, loaded: _Loaded, args: dict[str, Any]
    ) -> ExecutionResult:
        tool, server = loaded.tool, loaded.server
        # Audit BEFORE the side effect; a failed write aborts the call.
        rid = await anyio.to_thread.run_sync(
            self._audit, agent_id, tool.id, server.id, STARTED, "invoking"
        )
        t0 = time.perf_counter()
        outcome, detail, result = ERROR, "upstream invocation failed", None
        try:
            with anyio.fail_after(self._timeout_s):
                result = await self._invoker.call_tool(server, tool.name, args, self._timeout_s)
            outcome = ERROR if result.is_error else OK
            detail = "tool reported an error" if result.is_error else ""
        except TimeoutError:
            outcome, detail = TIMEOUT, f"timed out after {self._timeout_s:g}s"
        except ToolInvocationError as exc:
            outcome, detail = ERROR, redact(exc.curated)
        except anyio.get_cancelled_exc_class():
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(
                    self._finalize, rid, tool.id, CANCELLED, "caller cancelled", _ms(t0)
                )
            raise
        except Exception as exc:  # noqa: BLE001 — curated boundary: type name only, never str(exc)
            log.warning(
                "execution.upstream_failure agent=%s tool=%s exc_type=%s",
                scrub_log(agent_id),
                scrub_log(stable_tool_id(server.name, tool.name)),
                type(exc).__name__,
            )
        latency = _ms(t0)
        await anyio.to_thread.run_sync(self._finalize, rid, tool.id, outcome, detail, latency)
        return ExecutionResult(outcome, detail, rid, result=result if outcome != TIMEOUT else None)

    # ---------------------------------------------------------- approvals
    def _create_approval(self, loaded: _Loaded, args: dict[str, Any]) -> tuple[str, str]:
        now = self._clock()
        tool, server = loaded.tool, loaded.server
        with self._factory() as s:
            req = ApprovalRequest(
                agent_id=loaded.principal.agent_id,
                tool_id=tool.id,
                server_id=server.id,
                schema_hash=tool.schema_hash,
                arguments=args,
                summary={
                    "tool": stable_tool_id(server.name, tool.name),
                    "operation": effective_operation(tool.operation),
                    "arguments": redact_value(args),
                },
                status=APPROVAL_PENDING,
                created_at=now,
                expires_at=now + self._approval_ttl,
            )
            s.add(req)
            s.commit()
            approval_id = req.id
        rid = self._audit(
            loaded.principal.agent_id,
            tool.id,
            server.id,
            PENDING_APPROVAL,
            f"approval {approval_id} pending",
        )
        return approval_id, rid

    def _claim(self, approval_id: str) -> ApprovalRequest:
        now = self._clock()
        with self._factory() as s:
            claimed = s.scalars(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.id == approval_id,
                    ApprovalRequest.status == APPROVAL_PENDING,
                    ApprovalRequest.expires_at > now,
                )
                .values(status=APPROVAL_EXECUTING, decided_at=now)
                .returning(ApprovalRequest)
            ).one_or_none()
            if claimed is not None:
                s.commit()
                s.expunge(claimed)
                return claimed
            row = s.get(ApprovalRequest, approval_id)
            if row is None:
                raise ApprovalError("not_found", "approval not found")
            if row.status == APPROVAL_PENDING:  # pending but past expiry
                row.status, row.arguments, row.decided_at = APPROVAL_EXPIRED, None, now
                s.commit()
                raise ApprovalError("expired", "approval expired")
            if row.status == APPROVAL_EXPIRED:
                raise ApprovalError("expired", "approval expired")
            raise ApprovalError("already_decided", "approval already decided")

    def _close_approval(
        self, approval_id: str, status: str, record_id: str | None, preview: str | None
    ) -> None:
        with self._factory() as s:
            s.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == approval_id)
                .values(
                    status=status,
                    arguments=None,
                    execution_record_id=record_id,
                    result_preview=preview,
                )
            )
            s.commit()

    async def approve(self, approval_id: str) -> ExecutionResult:
        """Execute a pending approval exactly once. Raises ApprovalError."""
        req = await anyio.to_thread.run_sync(self._claim, approval_id)
        principal = AgentPrincipal(id="", agent_id=req.agent_id, key_hash="", enabled=True)
        loaded = await anyio.to_thread.run_sync(self._load, principal, req.tool_id)

        async def refuse(detail: str) -> ExecutionResult:
            rid = await anyio.to_thread.run_sync(self._refuse, req.agent_id, loaded, DENIED, detail)
            await anyio.to_thread.run_sync(self._close_approval, req.id, APPROVAL_FAILED, rid, None)
            return ExecutionResult(DENIED, detail, rid, approval_id=req.id)

        if loaded is None:
            return await refuse(f"approval {req.id}: tool or principal no longer exists")
        decision = evaluate(loaded.principal, loaded.server, loaded.tool, loaded.rules)
        if not decision.allow:
            return await refuse(f"approval {req.id}: {decision.reason}")
        if loaded.tool.schema_hash != req.schema_hash:
            return await refuse(f"approval {req.id}: tool schema changed since request")
        if not (loaded.tool.enabled and loaded.tool.available and loaded.server.enabled):
            return await refuse(f"approval {req.id}: tool unavailable")
        try:
            args = validate_arguments(loaded.tool.input_schema, req.arguments or {})
        except ArgumentValidationError as exc:
            return await refuse(f"approval {req.id}: arguments no longer valid: {exc.errors}")

        try:
            res = await self._invoke(req.agent_id, loaded, args)
        except BaseException:
            # Never leave raw arguments at rest in a wedged 'executing' row.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(
                    self._close_approval, req.id, APPROVAL_FAILED, None, None
                )
            raise
        preview = None
        if res.result is not None:
            texts = [str(c.get("text", "")) for c in res.result.content if c.get("type") == "text"]
            preview = redact("\n".join(texts))[:MAX_PREVIEW]
        final = APPROVAL_EXECUTED if res.status == OK else APPROVAL_FAILED
        await anyio.to_thread.run_sync(self._close_approval, req.id, final, res.record_id, preview)
        return ExecutionResult(res.status, res.detail, res.record_id, res.result, req.id)

    def _deny(self, approval_id: str) -> None:
        now = self._clock()
        with self._factory() as s:
            row = s.scalars(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.id == approval_id, ApprovalRequest.status == APPROVAL_PENDING
                )
                .values(status=APPROVAL_DENIED, decided_at=now, arguments=None)
                .returning(ApprovalRequest)
            ).one_or_none()
            if row is None:
                exists = s.get(ApprovalRequest, approval_id) is not None
                raise ApprovalError(
                    "already_decided" if exists else "not_found",
                    "approval already decided" if exists else "approval not found",
                )
            agent_id, tool_id, server_id = row.agent_id, row.tool_id, row.server_id
            s.commit()
        self._audit(agent_id, tool_id, server_id, DENIED, f"approval {approval_id} denied by admin")

    async def deny(self, approval_id: str) -> None:
        await anyio.to_thread.run_sync(self._deny, approval_id)

    def _get(self, approval_id: str, agent_id: str | None) -> ApprovalView | None:
        with self._factory() as s:
            row = s.get(ApprovalRequest, approval_id)
            if row is None or (agent_id is not None and row.agent_id != agent_id):
                return None
            return _view(row)

    async def get_approval(
        self, approval_id: str, agent_id: str | None = None
    ) -> ApprovalView | None:
        """agent_id given => only that agent's approval is visible."""
        return await anyio.to_thread.run_sync(self._get, approval_id, agent_id)

    def _list(self, status: str | None) -> list[ApprovalView]:
        with self._factory() as s:
            q = select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc()).limit(200)
            if status:
                q = q.where(ApprovalRequest.status == status)
            return [_view(r) for r in s.scalars(q).all()]

    async def list_approvals(self, status: str | None = None) -> list[ApprovalView]:
        return await anyio.to_thread.run_sync(self._list, status)


def _view(row: ApprovalRequest) -> ApprovalView:
    return ApprovalView(
        id=row.id,
        agent_id=row.agent_id,
        tool_id=row.tool_id,
        status=row.status,
        summary=dict(row.summary or {}),
        created_at=row.created_at,
        expires_at=row.expires_at,
        decided_at=row.decided_at,
        result_preview=row.result_preview,
    )


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0
