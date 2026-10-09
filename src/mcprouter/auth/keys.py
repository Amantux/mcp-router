"""Credential formats: agent-key parsing, hashing, Bearer parsing (P-607 move).

The raw key is never stored, logged, or echoed in an error.
"""

from __future__ import annotations

import hashlib
import re
import secrets

MAX_KEY_LEN = 512
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
_KEY_RE = re.compile(r"^[\x21-\x7e]{1,512}$")  # printable ASCII, no spaces


class AgentKeysConfigError(ValueError):
    """Malformed MCPR_AGENT_KEYS. The message never contains key material."""


class AuthenticationError(Exception):
    """No valid credential. Curated message; maps to 401."""

    def __init__(self, message: str = "invalid or missing API key") -> None:
        super().__init__(message)
        self.message = message


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return "mcpr_" + secrets.token_urlsafe(32)


def parse_agent_keys(spec: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for idx, raw in enumerate(p.strip() for p in spec.split(",")):
        if not raw:
            continue
        agent_id, sep, key = raw.partition(":")
        if not sep or not _AGENT_ID_RE.match(agent_id) or not _KEY_RE.match(key):
            raise AgentKeysConfigError(
                f"MCPR_AGENT_KEYS entry #{idx + 1} is malformed; expected 'agent_id:key' "
                "(agent_id [A-Za-z0-9_.-], key printable ASCII without spaces)"
            )
        if agent_id in seen:
            raise AgentKeysConfigError(f"MCPR_AGENT_KEYS lists agent '{agent_id}' twice")
        seen.add(agent_id)
        pairs.append((agent_id, key))
    return pairs


def parse_bearer(header: str | None) -> str | None:
    """None if absent; raises AuthenticationError if present but malformed."""
    if header is None:
        return None
    scheme, sep, token = header.partition(" ")
    if not sep or scheme.lower() != "bearer" or not _KEY_RE.match(token):
        raise AuthenticationError()
    return token
