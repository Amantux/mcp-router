"""Build real ``MCPServer`` instances (mcp SDK 2.x) from fleet specs.

Each tool advertises exactly ``ToolSpec.input_schema()`` and, when called,
returns a deterministic JSON echo — enough for discovery, routing and
execution tests without any real backend.
"""

from __future__ import annotations

import inspect
import json
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools import Tool
from mcp.types import ToolAnnotations
from pydantic import Field

from testbed.fleet import ServerSpec, ToolSpec

_JSON_TO_PY: dict[str, type] = {"string": str, "integer": int, "boolean": bool, "number": float}


def _tool_fn(server: str, spec: ToolSpec) -> Any:
    """A callable whose signature matches the spec's params (the SDK builds
    its argument validator from the signature)."""

    async def fn(**kwargs: Any) -> str:
        args = {k: v for k, v in kwargs.items() if v is not None}
        return json.dumps({"server": server, "tool": spec.name, "ok": True, "args": args})

    params: list[inspect.Parameter] = []
    annotations: dict[str, Any] = {}
    # KEYWORD_ONLY params may mix required/optional in any order, so spec
    # order is kept as-is (deterministic schemas).
    for p in spec.params:
        py: Any = _JSON_TO_PY[p.type]
        ann: Any = Annotated[py if p.required else py | None, Field(description=p.description)]
        default = inspect.Parameter.empty if p.required else None
        params.append(
            inspect.Parameter(
                p.name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=ann
            )
        )
        annotations[p.name] = ann
    fn.__signature__ = inspect.Signature(params, return_annotation=str)  # type: ignore[attr-defined]
    fn.__annotations__ = {**annotations, "return": str}
    fn.__name__ = spec.name
    return fn


def build_tool(server: str, spec: ToolSpec) -> Tool:
    tool = Tool.from_function(
        _tool_fn(server, spec),
        name=spec.name,
        description=spec.description,
        annotations=ToolAnnotations(
            read_only_hint=spec.operation == "read",
            destructive_hint=spec.destructive,
        ),
    )
    # Advertise the clean spec schema (no pydantic titles / anyOf-null noise),
    # which is what discovery hashes. Validation still uses the fn model.
    return tool.model_copy(update={"parameters": spec.input_schema()})


def build_server(spec: ServerSpec) -> MCPServer:
    return MCPServer(
        spec.name,
        version=spec.version,
        instructions=f"Synthetic {spec.service} server ({spec.domain}).",
        tools=[build_tool(spec.name, t) for t in spec.tools],
        log_level="WARNING",
    )
