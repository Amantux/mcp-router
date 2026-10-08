"""Admin-action audit log lines. Attacker-influenced fields (tool names come
from upstream MCP servers; justifications from humans) are CR/LF- and
control-char-scrubbed so they cannot forge extra log lines."""

from __future__ import annotations

import logging
import re

audit_log = logging.getLogger("mcprouter.audit")

_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_MAX_FIELD = 300


def scrub(value: object) -> str:
    s = _CTRL.sub(lambda m: f"\\x{ord(m.group()):02x}", str(value))
    return s if len(s) <= _MAX_FIELD else s[:_MAX_FIELD] + "..."


def audit(event: str, **fields: object) -> None:
    body = " ".join(f"{k}={scrub(v)!r}" for k, v in fields.items())
    audit_log.info("%s %s", event, body)
