"""Webhook delivery for approvals (P-109).

When an execution requires approval, the router POSTs one JSON webhook to a
configured operator URL (phone-push service such as ntfy.sh, Slack, or any
HTTPS endpoint), so the operator learns a write/execute action is waiting
without keeping the dashboard open.

Delivery rules:
- Outbound calls follow net_policy: https-only except localhost, forbidden
  addresses refused via resolve_and_check, secrets never logged, total deadline.
- Every failure is logged as warning and swallowed — delivery must never break
  approval creation.
- When MCPR_APPROVAL_WEBHOOK_SECRET is set, X-MCPR-Signature: sha256=<hex>
  header is added (HMAC-SHA256 of raw body).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import URLError

from mcprouter.net_policy import FORBIDDEN_NETS, LOOPBACK_HOSTS, resolve_and_check
from mcprouter.settings import Settings

log = logging.getLogger(__name__)

# Total deadline for webhook delivery (seconds)
_WEBHOOK_TIMEOUT_S = 10.0


def _is_localhost_host(host: str) -> bool:
    """Check if host is a loopback name (IPv4/IPv6 literal or DNS name)."""
    # Normalize IPv6 literals
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host in LOOPBACK_HOSTS


def _is_safe_url(url: str) -> bool:
    """Return True if url passes net_policy checks."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname

        # Must be https or http to localhost
        if scheme not in ("https", "http"):
            return False

        # HTTP only allowed to localhost
        if scheme == "http" and not _is_localhost_host(host or ""):
            return False

        # Check if host resolves to forbidden address
        if host:
            resolve_and_check(host)
        return True
    except Exception:
        return False


def _redact_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Deep redact arguments for safe logging/transmission."""
    if not isinstance(arguments, dict):
        return arguments

    redacted: dict[str, Any] = {}
    for key, value in arguments.items():
        if isinstance(value, dict):
            redacted[key] = _redact_args(value)
        elif isinstance(value, list):
            redacted[key] = [_redact_args(v) if isinstance(v, dict) else v for v in value]
        elif isinstance(value, str):
            # Short strings: mask all but first/last char
            if len(value) > 6:
                redacted[key] = value[0] + "*" * (len(value) - 2) + value[-1]
            else:
                redacted[key] = "*" * len(value)
        else:
            redacted[key] = value
    return redacted


def _format_datetime(dt: datetime) -> str:
    """Format datetime as ISO 8601 UTC string."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class WebhookPayload:
    """Payload sent to the webhook endpoint."""

    approval_id: str
    agent_id: str
    tool: str
    operation: str
    arguments: dict[str, Any]
    created_at: str
    expires_at: str
    approve_url: str
    deny_url: str

    def to_json(self) -> bytes:
        """Serialize to JSON bytes."""
        data = {
            "approval_id": self.approval_id,
            "agent_id": self.agent_id,
            "tool": self.tool,
            "operation": self.operation,
            "arguments": _redact_args(self.arguments),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "approve_url": self.approve_url,
            "deny_url": self.deny_url,
        }
        return json.dumps(data, separators=(",", ":")).encode("utf-8")


def _sign_payload(body: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 signature of body."""
    signature = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return f"sha256={signature}"


def deliver_webhook(
    payload: WebhookPayload,
    webhook_url: str | None,
    webhook_secret: str | None,
) -> None:
    """POST payload to webhook_url. Never raises; failures are logged and swallowed."""
    if not webhook_url:
        return

    # Validate URL before attempting delivery
    if not _is_safe_url(webhook_url):
        log.warning(
            "webhook.refused url=%s reason=unsafe",
            webhook_url,
        )
        return

    # Check if host resolves to forbidden address
    try:
        parsed = urlsplit(webhook_url)
        if parsed.hostname:
            resolve_and_check(parsed.hostname)
    except Exception as exc:
        log.warning(
            "webhook.refused url=%s reason=forbidden_address error=%s",
            webhook_url,
            type(exc).__name__,
        )
        return

    body = payload.to_json()

    # Build request
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "mcp-router/1.0",
    }
    if webhook_secret:
        headers["X-MCPR-Signature"] = _sign_payload(body, webhook_secret)

    req = Request(webhook_url, data=body, headers=headers, method="POST")

    # Attempt delivery with timeout
    t0 = time.perf_counter()
    try:
        with urlopen(req, timeout=_WEBHOOK_TIMEOUT_S) as resp:
            elapsed = (time.perf_counter() - t0) * 1000
            log.info(
                "webhook.delivered url=%s status=%d elapsed_ms=%.1f",
                webhook_url,
                resp.getcode(),
                elapsed,
            )
    except URLError as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        log.warning(
            "webhook.failed url=%s error=%s elapsed_ms=%.1f",
            webhook_url,
            type(exc).__name__,
            elapsed,
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        log.warning(
            "webhook.failed url=%s error=%s elapsed_ms=%.1f",
            webhook_url,
            type(exc).__name__,
            elapsed,
        )


def create_webhook_payload(
    approval_id: str,
    agent_id: str,
    tool_name: str,
    operation: str,
    arguments: dict[str, Any],
    created_at: datetime,
    expires_at: datetime,
    base_url: str,
) -> WebhookPayload:
    """Create a webhook payload for an approval."""
    # Build approve/deny URLs
    # These are relative paths; the operator is expected to prepend their base URL
    approve_path = f"/api/v1/approvals/{approval_id}/approve"
    deny_path = f"/api/v1/approvals/{approval_id}/deny"

    return WebhookPayload(
        approval_id=approval_id,
        agent_id=agent_id,
        tool=tool_name,
        operation=operation,
        arguments=arguments,
        created_at=_format_datetime(created_at),
        expires_at=_format_datetime(expires_at),
        approve_url=f"{base_url}{approve_path}",
        deny_url=f"{base_url}{deny_path}",
    )


def deliver_approval_webhook(
    approval_id: str,
    agent_id: str,
    tool_name: str,
    operation: str,
    arguments: dict[str, Any],
    created_at: datetime,
    expires_at: datetime,
    settings: Settings,
) -> None:
    """Fire webhook for an approval using app settings."""
    webhook_url = getattr(settings, "approval_webhook_url", "")
    if not webhook_url:
        return

    # Extract base URL from webhook_url for approve/deny paths
    try:
        parsed = urlsplit(webhook_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        base_url = webhook_url

    payload = create_webhook_payload(
        approval_id=approval_id,
        agent_id=agent_id,
        tool_name=tool_name,
        operation=operation,
        arguments=arguments,
        created_at=created_at,
        expires_at=expires_at,
        base_url=base_url,
    )

    webhook_secret = getattr(settings, "approval_webhook_secret", "")
    deliver_webhook(
        payload=payload,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
    )
