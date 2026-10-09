"""Log hygiene helpers.

Server names, tool names and anything else a remote server or an imported
config controls are attacker-influenced. `scrub` delegates to the ONE strong
sanitiser, `execution.redaction.scrub_log` (secret-shaped substrings masked;
CR/LF, ANSI escapes and every other control character neutralised), then caps
the length before the value reaches a log line (P-610).
"""

from __future__ import annotations

from collections.abc import Iterable

from mcprouter.execution.redaction import scrub_log

_MAX = 200


def scrub(value: object) -> str:
    text = scrub_log(str(value))
    return text if len(text) <= _MAX else text[:_MAX] + "..."


def mask_secrets(text: str, secrets: Iterable[str]) -> str:
    """Replace every occurrence of each (non-trivial) KNOWN secret value with
    ``***``. (Not a log sanitiser: pair it with `scrub` / `scrub_log`.)"""
    for s in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(s, "***")
    return text


# Old name; it collided with execution.redaction.redact (different semantics).
redact = mask_secrets
