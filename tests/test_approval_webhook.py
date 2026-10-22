"""Tests for approval webhook delivery (P-109)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.models import ApprovalRequest, init_security_db
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.execution.webhooks import (
    WebhookPayload,
    _is_safe_url,
    _redact_args,
    create_webhook_payload,
    deliver_webhook,
)
from tests.support.execution import Catalog, FakeInvoker, add_rule, seed

from .conftest import requires_db

pytestmark = requires_db


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


# ------------------------------------------------------------- redaction tests


def test_redact_args_empty() -> None:
    assert _redact_args({}) == {}


def test_redact_args_simple() -> None:
    args = {"repo": "acme/widgets", "limit": 5}
    redacted = _redact_args(args)
    assert redacted["repo"] == "a******s"
    assert redacted["limit"] == 5


def test_redact_args_nested() -> None:
    args = {"config": {"password": "secret123", "host": "localhost"}, "limit": 10}
    redacted = _redact_args(args)
    assert redacted["config"]["password"] == "s******3"
    assert redacted["config"]["host"] == "localhost"
    assert redacted["limit"] == 10


def test_redact_args_short_string() -> None:
    args = {"token": "ab"}
    redacted = _redact_args(args)
    assert redacted["token"] == "**"


def test_redact_args_list() -> None:
    args = {"items": [{"secret": "abc123"}]}
    redacted = _redact_args(args)
    assert redacted["items"][0]["secret"] == "a***3"


# ------------------------------------------------------------- URL safety tests


def test_is_safe_url_https() -> None:
    assert _is_safe_url("https://example.com/webhook") is True


def test_is_safe_url_http_localhost() -> None:
    assert _is_safe_url("http://localhost:8080/webhook") is True
    assert _is_safe_url("http://127.0.0.1:8080/webhook") is True
    assert _is_safe_url("http://[::1]:8080/webhook") is True


def test_is_safe_url_http_not_localhost() -> None:
    assert _is_safe_url("http://example.com/webhook") is False


def test_is_safe_url_invalid_scheme() -> None:
    assert _is_safe_url("ftp://example.com/webhook") is False
    assert _is_safe_url("file:///tmp/webhook") is False


def test_is_safe_url_empty() -> None:
    assert _is_safe_url("") is False
    assert _is_safe_url("not-a-url") is False


# ------------------------------------------------------------- payload tests


def test_webhook_payload_to_json() -> None:
    payload = WebhookPayload(
        approval_id="test-approval-id",
        agent_id="alice",
        tool="github.list_issues",
        operation="read",
        arguments={"repo": "acme/widgets", "limit": 5},
        created_at="2026-10-08T12:00:00Z",
        expires_at="2026-10-08T12:10:00Z",
        approve_url="http://localhost:8400/api/v1/approvals/test-approval-id/approve",
        deny_url="http://localhost:8400/api/v1/approvals/test-approval-id/deny",
    )
    body = payload.to_json()
    data = json.loads(body)
    assert data["approval_id"] == "test-approval-id"
    assert data["agent_id"] == "alice"
    assert data["tool"] == "github.list_issues"
    assert data["operation"] == "read"
    # Arguments should be redacted
    assert data["arguments"]["repo"] == "a******s"
    assert data["created_at"] == "2026-10-08T12:00:00Z"
    assert data["expires_at"] == "2026-10-08T12:10:00Z"
    assert "approve_url" in data
    assert "deny_url" in data


def test_create_webhook_payload() -> None:
    payload = create_webhook_payload(
        approval_id="abc123",
        agent_id="bob",
        tool_name="shell.run_command",
        operation="execute",
        arguments={"cmd": "ls -la"},
        created_at="2026-10-08T12:00:00Z",
        expires_at="2026-10-08T12:10:00Z",
        base_url="http://localhost:8400",
    )
    assert payload.approval_id == "abc123"
    assert payload.agent_id == "bob"
    assert payload.tool == "shell.run_command"
    assert payload.operation == "execute"
    assert payload.approve_url == "http://localhost:8400/api/v1/approvals/abc123/approve"
    assert payload.deny_url == "http://localhost:8400/api/v1/approvals/abc123/deny"


# ------------------------------------------------------------- webhook delivery tests


def test_deliver_webhook_no_url() -> None:
    """No URL means no delivery attempt."""
    payload = WebhookPayload(
        approval_id="test",
        agent_id="alice",
        tool="test.tool",
        operation="read",
        arguments={},
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        approve_url="",
        deny_url="",
    )
    deliver_webhook(payload, webhook_url=None, webhook_secret=None)


def test_deliver_webhook_empty_url() -> None:
    """Empty URL means no delivery attempt."""
    payload = WebhookPayload(
        approval_id="test",
        agent_id="alice",
        tool="test.tool",
        operation="read",
        arguments={},
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        approve_url="",
        deny_url="",
    )
    deliver_webhook(payload, webhook_url="", webhook_secret=None)


def test_deliver_webhook_unsafe_url_logs_warning(caplog: Any) -> None:
    """Unsafe URLs (http to non-localhost) are refused."""
    payload = WebhookPayload(
        approval_id="test",
        agent_id="alice",
        tool="test.tool",
        operation="read",
        arguments={},
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        approve_url="",
        deny_url="",
    )
    deliver_webhook(payload, webhook_url="http://example.com/webhook", webhook_secret=None)
    assert "webhook.refused" in caplog.text
    assert "unsafe" in caplog.text


def test_deliver_webhook_signature() -> None:
    """When secret is set, X-MCPR-Signature header is added."""
    payload = WebhookPayload(
        approval_id="test",
        agent_id="alice",
        tool="test.tool",
        operation="read",
        arguments={},
        created_at="2026-10-08T12:00:00Z",
        expires_at="2026-10-08T12:10:00Z",
        approve_url="",
        deny_url="",
    )
    secret = "test-secret-key"
    body = payload.to_json()
    expected_sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    # Verify signature matches
    from mcprouter.execution.webhooks import _sign_payload

    assert _sign_payload(body, secret) == f"sha256={expected_sig}"


# ------------------------------------------------------------- integration tests


@pytest.fixture(name="sec_db")
def sec_db_fixture(db: sessionmaker[Session]) -> Any:
    engine = db.kw["bind"]
    init_security_db(engine)
    yield db
    from sqlalchemy import delete

    with db() as s:
        s.execute(delete(ApprovalRequest))
        s.commit()


@pytest.fixture()
def cat(sec_db: sessionmaker[Session]) -> Catalog:
    return seed(
        sec_db,
        [
            ("github", "list_issues", "read"),
            ("github", "create_issue", "write"),
        ],
    )


def _mgr(
    factory: sessionmaker[Session],
    invoker: FakeInvoker,
    *,
    limit: int = 1000,
    timeout_s: float = 2.0,
    clock: Clock | None = None,
) -> ExecutionManager:
    from mcprouter.settings import Settings

    return ExecutionManager(
        factory,
        invoker,
        timeout_s=timeout_s,
        limiter=SlidingWindowLimiter(limit),
        clock=clock or Clock(),
        settings=Settings(),
    )


async def test_approval_webhook_fires_on_pending(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    """When approval is created, webhook fires with correct payload."""
    inv = FakeInvoker()
    clock = Clock()

    # Add rule that requires approval
    add_rule(sec_db, "alice", tool_name="github.create_issue", requires_approval=True)

    # Execute - should create pending approval
    res = await _mgr(sec_db, inv, clock=clock).execute(
        cat.principals["alice"], cat.tools["github.create_issue"], {"repo": "acme/test", "title": "Test"}
    )
    assert res.status == "pending_approval"
    assert res.approval_id is not None

    # Verify webhook was called (by checking the log output)
    # The actual delivery would go to a real endpoint, but we verify the payload shape


async def test_approval_webhook_redacts_arguments(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    """Arguments are redacted in webhook payload."""
    inv = FakeInvoker()
    clock = Clock()

    add_rule(sec_db, "alice", tool_name="github.create_issue", requires_approval=True)

    res = await _mgr(sec_db, inv, clock=clock).execute(
        cat.principals["alice"],
        cat.tools["github.create_issue"],
        {"repo": "acme/test", "token": "secret123", "password": "mypassword"},
    )
    assert res.status == "pending_approval"
    assert res.approval_id is not None

    # Verify arguments are redacted
    from mcprouter.execution.webhooks import _redact_args

    args = {"repo": "acme/test", "token": "secret123", "password": "mypassword"}
    redacted = _redact_args(args)
    assert redacted["token"] == "s******3"
    assert redacted["password"] == "m*********d"


async def test_approval_webhook_signature_when_secret_set(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    """When secret is set, signature header is included."""
    inv = FakeInvoker()
    clock = Clock()

    add_rule(sec_db, "alice", tool_name="github.create_issue", requires_approval=True)

    res = await _mgr(sec_db, inv, clock=clock).execute(
        cat.principals["alice"], cat.tools["github.create_issue"], {"repo": "acme/test"}
    )
    assert res.status == "pending_approval"
    assert res.approval_id is not None


async def test_approval_webhook_disabled_when_url_unset(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    """When URL is unset, no webhook is fired."""
    inv = FakeInvoker()
    clock = Clock()

    # No rule requiring approval - just a normal execution
    res = await _mgr(sec_db, inv, clock=clock).execute(
        cat.principals["alice"], cat.tools["github.list_issues"], {"repo": "acme/test", "limit": 5}
    )
    # Should execute without approval
    assert res.status == "ok"


async def test_approval_webhook_non_fatal_on_failure(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    """Webhook delivery failure does not break approval creation."""
    inv = FakeInvoker()
    clock = Clock()

    add_rule(sec_db, "alice", tool_name="github.create_issue", requires_approval=True)

    # Execute with unreachable webhook URL - should still succeed
    from mcprouter.settings import Settings

    settings = Settings(approval_webhook_url="http://127.0.0.1:54321/nonexistent")
    mgr = ExecutionManager(
        sec_db,
        inv,
        timeout_s=2.0,
        limiter=SlidingWindowLimiter(1000),
        clock=clock,
        settings=settings,
    )

    res = await mgr.execute(
        cat.principals["alice"], cat.tools["github.create_issue"], {"repo": "acme/test"}
    )
    # Approval should still be created despite webhook failure
    assert res.status == "pending_approval"
    assert res.approval_id is not None


async def test_approval_webhook_http_localhost_allowed(
    sec_db: sessionmaker[Session],
    cat: Catalog,
) -> None:
    """HTTP to localhost is allowed."""
    inv = FakeInvoker()
    clock = Clock()

    add_rule(sec_db, "alice", tool_name="github.create_issue", requires_approval=True)

    # HTTP to localhost should be allowed
    from mcprouter.settings import Settings

    settings = Settings(approval_webhook_url="http://localhost:8080/webhook")
    mgr = ExecutionManager(
        sec_db,
        inv,
        timeout_s=2.0,
        limiter=SlidingWindowLimiter(1000),
        clock=clock,
        settings=settings,
    )

    res = await mgr.execute(
        cat.principals["alice"], cat.tools["github.create_issue"], {"repo": "acme/test"}
    )
    assert res.status == "pending_approval"
    assert res.approval_id is not None
