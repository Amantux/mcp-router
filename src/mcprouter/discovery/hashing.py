"""Canonical JSON + fingerprints for change detection.

Canonical form: keys sorted, no insignificant whitespace, UTF-8 kept as-is
(``ensure_ascii=False``) so a schema's hash never depends on dict insertion
order or the server's JSON formatting.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def schema_hash(input_schema: dict[str, Any]) -> str:
    """``MCPToolRecord.schema_hash``: sha256 of the canonical input schema."""
    return sha256_hex(canonical_json(input_schema))


def metadata_fingerprint(
    description: str, title: str | None, annotations: dict[str, Any] | None
) -> str:
    """Everything except the schema that, when changed, is a ``metadata`` change.

    Annotations are included on purpose: a flip of ``readOnlyHint`` changes how
    the tool should be classified (read vs write) and must be versioned.
    """
    return sha256_hex(
        canonical_json({"description": description, "title": title, "annotations": annotations})
    )
