"""Token-cost estimate for a tool definition as an agent would receive it.

HEURISTIC, stated plainly: tokens ~= ceil(chars / 4) over the compact JSON
of `{"name", "description", "inputSchema"}` — the three fields the gateway
renders into `tools/list` (`name` is the stable `server.tool` id; a
non-object schema is rendered as `{"type": "object"}`, mirroring the
gateway). No tokenizer is loaded: ~4 chars/token is the usual rule of thumb
for English + JSON on BPE tokenizers, and real counts typically land within
roughly +-25% of it, varying by model. Absolute token numbers are therefore
ESTIMATES. The savings RATIO is far less sensitive, because exposed and
catalog tokens use the same estimator over the same kind of text.

Not counted: the `find_tools` meta-tool (sent either way), approval
suffixes, MCP framing, and redaction-induced length changes.
"""

from __future__ import annotations

import json
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from mcprouter.models import SKILL_ID_PREFIX, MCPServerRecord, MCPToolRecord, SkillRecord

CHARS_PER_TOKEN = 4
ESTIMATOR = "chars/4 over compact JSON of name+description+inputSchema"


def rendered_definition(server_name: str, tool_name: str, description: str, schema: Any) -> str:
    sch = schema if isinstance(schema, dict) and schema.get("type") == "object" else None
    return json.dumps(
        {
            "name": f"{server_name}.{tool_name}",
            "description": description or "",
            "inputSchema": sch if sch is not None else {"type": "object"},
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )


def estimate_tokens(server_name: str, tool_name: str, description: str, schema: Any) -> int:
    chars = len(rendered_definition(server_name, tool_name, description, schema))
    return math.ceil(chars / CHARS_PER_TOKEN)


def tool_token_map(session: Session) -> dict[str, int]:
    """tool_id -> estimated tokens, for every tool currently in the catalog
    (one pass; ~1k rows at the scale target)."""
    rows = session.execute(
        select(
            MCPToolRecord.id,
            MCPServerRecord.name,
            MCPToolRecord.name,
            MCPToolRecord.description,
            MCPToolRecord.input_schema,
        ).join(MCPServerRecord, MCPServerRecord.id == MCPToolRecord.server_id)
    ).all()
    return {tid: estimate_tokens(srv, name, desc, sch) for tid, srv, name, desc, sch in rows}


def skill_metadata_tokens(name: str, description: str) -> int:
    """The progressive-disclosure tier an agent always receives for a skill:
    `name` + `description` (the body is sent only on activation). Estimator:
    chars/4 over the compact JSON of those two fields."""
    chars = len(
        json.dumps(
            {"name": name, "description": description or ""},
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
        )
    )
    return math.ceil(chars / CHARS_PER_TOKEN)


def skill_token_maps(session: Session) -> tuple[dict[str, int], dict[str, int]]:
    """("skill:<id>" -> metadata tokens, "skill:<id>" -> body_tokens_est) for
    every skill currently in the catalog. Keys use the funnel's kind prefix."""
    rows = session.execute(
        select(
            SkillRecord.id, SkillRecord.name, SkillRecord.description, SkillRecord.body_tokens_est
        )
    ).all()
    meta = {f"{SKILL_ID_PREFIX}{sid}": skill_metadata_tokens(n, d) for sid, n, d, _ in rows}
    body = {f"{SKILL_ID_PREFIX}{sid}": int(b or 0) for sid, _, _, b in rows}
    return meta, body
