"""Shared seed helpers for registry/tools-api/dedup tests (direct ORM inserts —
live discovery belongs to the discovery track). No tests live here."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from mcprouter.models import EMBEDDING_DIM, MCPServerRecord, MCPToolRecord


def make_server(s: Session, name: str = "srv") -> MCPServerRecord:
    srv = MCPServerRecord(name=name, transport="stdio", stdio_command=["true"])
    s.add(srv)
    s.flush()
    return srv


def props_schema(*names: str) -> dict[str, Any]:
    return {"type": "object", "properties": {n: {"type": "string"} for n in names}}


def make_tool(
    s: Session,
    server: MCPServerRecord,
    name: str,
    description: str = "",
    **kw: Any,
) -> MCPToolRecord:
    schema = kw.pop("input_schema", {})
    tool = MCPToolRecord(
        server_id=server.id,
        name=name,
        description=description,
        input_schema=schema,
        schema_hash=hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
        **kw,
    )
    s.add(tool)
    s.flush()
    return tool


def unit_vec(*hot: tuple[int, float]) -> list[float]:
    """Sparse 384-dim vector from (index, value) pairs."""
    v = [0.0] * EMBEDDING_DIM
    for i, x in hot:
        v[i] = x
    return v
