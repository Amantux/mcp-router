"""Change-detection hashes for skills (mirrors discovery/hashing.py's role)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any


def content_hash(skill_md: bytes) -> str:
    return hashlib.sha256(skill_md).hexdigest()


def manifest_hash(manifest: Iterable[Mapping[str, Any]]) -> str:
    """sha256 over sorted "path\\0sha256" lines; oversize entries hash as "oversize:<size>"."""
    lines = sorted(
        f"{e['path']}\0{e.get('sha256') or 'oversize:' + str(e['size'])}" for e in manifest
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
