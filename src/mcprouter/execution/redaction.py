"""Secret redaction and log-field scrubbing (FR-07).

`redact()` runs on EVERYTHING that leaves the process boundary as text we did
not author: log fields, audit detail, approval summaries, and model inputs.

ReDoS discipline: every pattern here is linear-time. Unbounded quantifiers are
only used where the following token cannot overlap the quantified class, and
the token runs are anchored with look-behind/look-ahead so a scan never
restarts in the middle of a run (each start position fails in O(1)).
`test_no_catastrophic_backtracking` pins this.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

MASK = "***"

# Keys whose VALUES are secrets regardless of shape (matched case-insensitively,
# as a suffix so `db_password`, `github_token`, `x-api-key` all hit).
_SECRET_KEY_RE = re.compile(
    r"(?:token|passw(?:or)?d|pwd|secret|api[_-]?key|apikey|authorization|"
    r"credentials?|private[_-]?key|session[_-]?id|cookie)s?$",
    re.IGNORECASE,
)

# scheme://user:pass@host  or  scheme://:pass@host  or scheme://token@host
_URL_USERINFO_RE = re.compile(r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.\-]{0,30}://)[^\s/@?#]+@")

_BEARER_RE = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=\-]+")

# key=value / key: value / "key": "value"  (value up to a delimiter)
_KV_RE = re.compile(
    # Look-behind pins the key to an identifier START: without it, the lazy
    # prefix is retried at every offset inside a long run (measured 1.4s on
    # 200k chars). With it, mid-run starts fail in O(1).
    r"(?i)(?<![A-Za-z0-9_.\-])(?P<key>[A-Za-z0-9_.\-]{0,40}?"
    r"(?:token|passw(?:or)?d|pwd|secret|api[_-]?key|apikey|authorization|credentials?|"
    r"private[_-]?key|session[_-]?id|cookie)s?)"
    r"(?P<sep>[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<val>[^\s\"'&,;}\]]+)"
)

# Provider-specific prefixes (shape is enough; no entropy check needed).
_PREFIXED_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])(?:"
    r"gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9_\-]{16,}"
    r"|xox[abposr]-[A-Za-z0-9\-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}"
    r")"
)

# Candidate high-entropy runs (judged by _looks_secret). Anchored both sides so
# matching is linear: greedy run, look-ahead succeeds at the run's end.
_RUN_CLASS = r"A-Za-z0-9+/=_\-"
_RUN_RE = re.compile(rf"(?<![{_RUN_CLASS}])[{_RUN_CLASS}]{{32,}}(?![{_RUN_CLASS}])")

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f  \u0085]")
_CONTROL_NAMES = {"\r": "\\r", "\n": "\\n", "\t": "\\t"}


def _entropy(s: str) -> float:
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _looks_secret(token: str) -> bool:
    if _UUID_RE.match(token):
        return False
    classes = sum(
        (
            any(ch.isdigit() for ch in token),
            any(ch.islower() for ch in token),
            any(ch.isupper() for ch in token),
        )
    )
    # Long words / snake_case identifiers are single-class or low-entropy.
    return classes >= 2 and _entropy(token) >= 3.5


def _mask_run(m: re.Match[str]) -> str:
    tok = m.group(0)
    return MASK if _looks_secret(tok) else tok


def redact(text: str) -> str:
    """Mask secret-shaped substrings in free text."""
    if not text:
        return text
    out = _URL_USERINFO_RE.sub(lambda m: f"{m.group('scheme')}{MASK}@", text)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)} {MASK}", out)
    out = _KV_RE.sub(lambda m: f"{m.group('key')}{m.group('sep')}{MASK}", out)
    out = _PREFIXED_RE.sub(MASK, out)
    out = _RUN_RE.sub(_mask_run, out)
    return out


def is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key))


def redact_value(value: Any, *, max_str: int = 200) -> Any:
    """Deep-copy `value` with secret-named keys masked and strings redacted.

    Strings are truncated to `max_str` characters (approval summaries and
    audit detail are previews, not payloads).
    """
    if isinstance(value, dict):
        return {
            k: (
                MASK
                if isinstance(k, str) and is_secret_key(k)
                else redact_value(v, max_str=max_str)
            )
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_value(v, max_str=max_str) for v in value]
    if isinstance(value, str):
        s = redact(value)
        return s if len(s) <= max_str else s[:max_str] + "…"
    return value


def escape_controls(text: str) -> str:
    """CR/LF, tabs, ANSI escapes, NEL and the Unicode line/paragraph separators
    become visible escapes: the ONE control-character class for log lines."""
    return _CONTROL_RE.sub(
        lambda m: _CONTROL_NAMES.get(m.group(0), f"\\x{ord(m.group(0)):02x}"), text
    )


def scrub_log(text: str) -> str:
    """Redact, then neutralize CR/LF and other control characters (log forging)."""
    return escape_controls(redact(text))
