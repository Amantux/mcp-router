"""Admin-action audit log lines. Attacker-influenced fields (tool names come
from upstream MCP servers; justifications from humans) are scrubbed so they
cannot forge extra log lines or fake fields."""

from __future__ import annotations

import logging

audit_log = logging.getLogger("mcprouter.audit")

_MAX_FIELD = 300


def scrub(value: object) -> str:
    """Truncate, then quote with repr(): CR/LF, other control and
    non-printable characters come out escaped (`\\n`, `\\x1b`, `\\u2028`) and
    the value is delimited, so it can neither break the line nor pose as
    another `key=value` field."""
    s = str(value)
    if len(s) > _MAX_FIELD:
        s = s[:_MAX_FIELD] + "..."
    return repr(s)


def audit(event: str, **fields: object) -> None:
    body = " ".join(f"{k}={scrub(v)}" for k, v in fields.items())
    audit_log.info("%s %s", event, body)
