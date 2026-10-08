"""Log hygiene helpers.

Server names, tool names and anything else a remote server or an imported
config controls are attacker-influenced: CR/LF are scrubbed (log forging)
and length is capped before they reach a log line.
"""

from __future__ import annotations

from collections.abc import Iterable

_MAX = 200


def scrub(value: object) -> str:
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    return text if len(text) <= _MAX else text[:_MAX] + "..."


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every occurrence of each (non-trivial) secret value with ``***``."""
    for s in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(s, "***")
    return text
