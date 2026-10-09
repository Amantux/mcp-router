"""FR-06 dynamic exposure as a standards-compatible MCP endpoint (mcp SDK 2.3).

Why the LOW-LEVEL `mcp.server.lowlevel.Server` and not `MCPServer`:
`MCPServer` keeps ONE process-wide ToolManager (`add_tool`/`remove_tool`
mutate what every session sees), so per-session tool lists would fight it.
The low-level Server takes `on_list_tools` / `on_call_tool` handlers that run
per request with a `ServerRequestContext`, whose `.request` is the Starlette
request — so each request is answered for the principal that sent it.

Request flow:
  HTTP -> _AuthASGI (Bearer -> principal via deps_auth.resolve_principal,
          401 otherwise; sets scope["user"] so the SDK binds each MCP session
          to the credential that created it) -> SDK streamable-HTTP app
          (/mcp, DNS-rebinding protection) -> handlers below.

* tools/list  = the agent's last route() result (or a deterministic default:
  top-N most-used tools), RE-FILTERED through policy, capped by the
  routing.budgets clamp chain: min(principal.max_tools, max_exposed_tools)
  tools over at most min(principal.max_servers, max_exposed_servers)
  DISTINCT servers (in exposure order; None = unlimited). Stable names
  `server_name.tool_name`. cacheScope=private, ttlMs=0.
* tools/call  = always via ExecutionManager.execute (policy, rate limit,
  validation, approval, audit). Never calls an upstream directly. A tool
  that is authorized but not currently exposed is still callable (clients
  that cache tool lists keep working); exposure is not an authz boundary.
* router.find_tools (meta tool, only when a RouteFn is wired): re-routes the
  agent's exposure from a natural-language query (redacted before it reaches
  the routing model), then emits tools/list_changed.
* tools/list_changed: handshake-era sessions get it on their standalone GET
  stream (`ServerSession.send_tool_list_changed`); 2026-07-28-era clients get
  it via `subscriptions/listen` on a PER-AGENT bus (one agent's re-route is
  never announced to another agent's streams).
"""

from __future__ import annotations

import base64
import contextlib
import inspect
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
import mcp_types as types
from fastapi import FastAPI
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.session import ServerSession
from mcp.server.subscriptions import (
    InMemorySubscriptionBus,
    ListenHandler,
    PromptsListChanged,
    ResourcesListChanged,
    ToolsListChanged,
)
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.authentication import AuthCredentials
from starlette.datastructures import Headers
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from mcprouter.analytics import feedback as _fb
from mcprouter.api.deps_auth import AuthenticationError, SecurityConfig, hash_key, resolve_principal
from mcprouter.execution.manager import ExecutionManager, ExecutionResult, stable_tool_id
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.execution.redaction import redact, scrub_log
from mcprouter.gateway.exposure import ExposureStore
from mcprouter.gateway.skills import (
    Activation,
    SkillAccessError,
    SkillExposure,
    prompt_name,
    resource_uri,
)
from mcprouter.interfaces import RouteFn, RouteRequest, RouteResult
from mcprouter.models import AgentPrincipal, MCPServerRecord, MCPToolRecord, PolicyRule
from mcprouter.policy.engine import evaluate
from mcprouter.routing.budgets import effective_budgets
from mcprouter.settings import Settings
from mcprouter.skills.serve import ResourceContent, manifest_entries, resource_mime

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
META_TOOL = "router.find_tools"
PRINCIPAL_SCOPE_KEY = "mcprouter.principal"
MAX_QUERY_LEN = 2000
MAX_TRACKED_SESSIONS_PER_AGENT = 32
NOTIFY_TIMEOUT_S = 2.0
ROUTE_REQUEST_ID_KWARG = "route_request_id"


def _accepts_kwarg(fn: Callable[..., object], name: str) -> bool:
    """Feature-detect an optional keyword (cross-branch seam: the analytics
    track adds `route_request_id` to ExecutionManager.execute)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    p = params.get(name)
    if p is not None:
        return p.kind in (inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    return any(q.kind is inspect.Parameter.VAR_KEYWORD for q in params.values())


_META_TOOL_DEF = types.Tool(
    name=META_TOOL,
    description=(
        "Find tools for a task. Describe what you want to do in plain language; the tool list "
        "is replaced with the most relevant tools you are authorized to use."
    ),
    input_schema={
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_LEN}},
        "required": ["query"],
        "additionalProperties": False,
    },
)

# router.feedback: the agent tells us which surfaced tools helped. Thin wrapper
# over analytics.feedback.record_feedback (the ONLY implementation, shared with
# POST /api/v1/route/{id}/feedback). Listed whenever router.find_tools is, and
# like it is appended AFTER the max_tools cap (never counts against it).
FEEDBACK_TOOL = "router.feedback"
_FEEDBACK_TOOL_DEF = types.Tool(
    name=FEEDBACK_TOOL,
    description=(
        "Report whether tools surfaced for you were helpful. requestId defaults to your "
        "latest routing decision. Each item names a surfaced tool or skill."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "requestId": {"type": "string", "maxLength": 36},
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "maxLength": 300},
                        "kind": {"type": "string", "enum": ["tool", "skill"]},
                        "helpful": {"type": "boolean"},
                        "note": {"type": "string", "maxLength": 2000},
                    },
                    "required": ["name", "helpful"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["items"],
        "additionalProperties": False,
    },
)


ACTIVATE_SKILL_TOOL = "router.activate_skill"
READ_SKILL_RESOURCE_TOOL = "router.read_skill_resource"
_SKILL_URI_PREFIX = "skill://"
# Curated by error code: never echo a path, skill id or serve-layer message.
_SKILL_ERRORS: dict[str, str] = {
    "rate_limited": "Too many skill activations; retry later.",
    "denied": "Skill access denied by policy.",
    "too_large": "Skill resource is too large.",
}
_SKILL_UNKNOWN = "Unknown skill or resource."
_SKILL_INTERNAL = "Internal error while serving the skill."

_SKILL_TOOL_DEFS = [
    types.Tool(
        name=ACTIVATE_SKILL_TOOL,
        description="Activate a routed skill by its prompt name (<source>/<skill>); "
        "returns the skill's instructions and its resource list.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 512}},
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name=READ_SKILL_RESOURCE_TOOL,
        description="Read one resource file of a routed skill (path relative to the skill).",
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 512},
                "path": {"type": "string", "minLength": 1, "maxLength": 1024},
            },
            "required": ["name", "path"],
            "additionalProperties": False,
        },
    ),
]


def _skill_error(exc: SkillAccessError) -> str:
    return _SKILL_ERRORS.get(exc.code, _SKILL_UNKNOWN)


def _skill_mcp_error(exc: Exception) -> MCPError:
    """Curated MCP error for any skill-path failure; never `str(exc)`."""
    if isinstance(exc, SkillAccessError) and exc.code != "internal":
        return MCPError(types.INVALID_PARAMS, _skill_error(exc))
    if not isinstance(exc, SkillAccessError):
        log.warning("gateway.skill_failed exc_type=%s", type(exc).__name__)
    return MCPError(types.INTERNAL_ERROR, _SKILL_INTERNAL)


def _parse_skill_uri(uri: str) -> tuple[str, str] | None:
    """`skill://<source>/<skill>/<path>` -> (prompt name, path). No decoding:
    the path is validated by SkillFiles (normalize_relpath + manifest)."""
    if not uri.startswith(_SKILL_URI_PREFIX):
        return None
    parts = uri[len(_SKILL_URI_PREFIX) :].split("/", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return f"{parts[0]}/{parts[1]}", parts[2]


class _RouterMCPServer(Server[Any]):
    """Low-level Server that advertises tools.listChanged on the handshake era.

    The SDK's session manager builds InitializationOptions by calling
    `create_initialization_options()` with no NotificationOptions, which would
    advertise listChanged=false; we default it to true. (On 2026-07-28 the flag
    derives from the subscriptions/listen handler being registered.)
    """

    def create_initialization_options(
        self,
        notification_options: NotificationOptions | None = None,
        experimental_capabilities: dict[str, dict[str, Any]] | None = None,
        extensions: dict[str, dict[str, Any]] | None = None,
    ) -> InitializationOptions:
        return super().create_initialization_options(
            notification_options
            or NotificationOptions(
                tools_changed=True, prompts_changed=True, resources_changed=True
            ),
            experimental_capabilities,
            extensions,
        )


def _text(text: str, *, is_error: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=is_error
    )


def _to_call_result(res: ExecutionResult) -> types.CallToolResult:
    if res.status in ("ok", "error") and res.result is not None:
        try:
            return types.CallToolResult.model_validate(
                {
                    "content": res.result.content,
                    "isError": res.result.is_error,
                    **(
                        {"structuredContent": res.result.structured_content}
                        if res.result.structured_content is not None
                        else {}
                    ),
                }
            )
        except ValueError:  # pydantic ValidationError subclasses ValueError
            return _text("Refused: upstream returned malformed content", is_error=True)
    if res.status == "pending_approval":
        return _text(
            f"Approval required: request {res.approval_id} is pending an administrator. "
            f"Poll GET /api/v1/me/approvals/{res.approval_id} for the outcome.",
            is_error=True,
        )
    return _text(f"Refused ({res.status}): {res.detail}", is_error=True)


class _AuthASGI:
    """Authenticates every HTTP request to /mcp before the SDK sees it."""

    def __init__(self, inner: ASGIApp, gateway: GatewayServer) -> None:
        self._inner = inner
        self._gw = gateway

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Fail closed by construction: only authenticated HTTP reaches the SDK.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        authorization = Headers(scope=scope).get("authorization")
        try:
            principal = await anyio.to_thread.run_sync(self._gw._authenticate, authorization)
        except AuthenticationError as exc:
            body = json.dumps({"error": "invalid_token", "error_description": exc.message}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        scope = dict(scope)
        # The SDK's session manager compares (client_id, issuer, subject) of
        # scope["user"] against the session creator: a session id minted for
        # agent A answers 404 to agent B. The token field holds a HASH — the
        # raw key never sits in request state.
        token = AccessToken(
            token=hash_key(authorization or "dev"), client_id=principal.agent_id, scopes=[]
        )
        scope["user"] = AuthenticatedUser(token)
        scope["auth"] = AuthCredentials([])
        scope[PRINCIPAL_SCOPE_KEY] = principal
        await self._inner(scope, receive, send)


class GatewayServer:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        security: SecurityConfig,
        settings: Settings,
        manager: ExecutionManager,
        route_fn: RouteFn | None = None,
        transport_security: TransportSecuritySettings | None = None,
        host: str = "127.0.0.1",
        skills: SkillExposure | None = None,
    ) -> None:
        self._factory = session_factory
        self._skills = skills
        # Routed skill ids per agent (RoutedTool.kind == "skill" of the last route).
        self._skill_ids: dict[str, tuple[str, ...]] = {}
        self._security = security
        self._settings = settings
        self._manager = manager
        self._manager_takes_route_id = _accepts_kwarg(manager.execute, ROUTE_REQUEST_ID_KWARG)
        self._route_fn = route_fn
        self.exposure = ExposureStore()
        self._route_limiter = SlidingWindowLimiter(settings.rate_limit_per_agent_per_min)
        self._buses: dict[str, tuple[InMemorySubscriptionBus, ListenHandler]] = {}
        self._legacy: dict[str, OrderedDict[str, ServerSession]] = {}
        self._lock = threading.Lock()
        self.server = _RouterMCPServer(
            "mcp-router",
            version="0.4.0",
            instructions=(
                "Tools are exposed per agent and change as you work. Call "
                f"{META_TOOL} with a task description to get relevant tools."
            ),
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
            on_subscriptions_listen=self._on_listen,
            on_list_prompts=self._on_list_prompts,
            on_get_prompt=self._on_get_prompt,
            on_list_resources=self._on_list_resources,
            on_read_resource=self._on_read_resource,
        )
        self._starlette = self.server.streamable_http_app(
            streamable_http_path=MCP_PATH, transport_security=transport_security, host=host
        )

    # ------------------------------------------------------------ wiring
    def asgi_app(self) -> ASGIApp:
        return _AuthASGI(self._starlette, self)

    def mount(self, app: FastAPI) -> None:
        """Add POST/GET/DELETE /mcp to `app` and run the SDK session manager
        inside the app's lifespan (mounted sub-app lifespans never run)."""
        app.router.routes.append(Route(MCP_PATH, endpoint=self.asgi_app()))
        inner_lifespan = app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def lifespan(a: Any) -> AsyncIterator[Any]:
            async with self.server.session_manager.run(), inner_lifespan(a) as state:
                yield state

        app.router.lifespan_context = lifespan

    # ------------------------------------------------------------ identity
    def _authenticate(self, authorization: str | None) -> AgentPrincipal:
        with self._factory() as s:
            return resolve_principal(s, self._security, authorization)

    def _principal(self, ctx: ServerRequestContext[Any, Any]) -> AgentPrincipal:
        request = ctx.request
        principal = getattr(request, "scope", {}).get(PRINCIPAL_SCOPE_KEY) if request else None
        if not isinstance(principal, AgentPrincipal):
            # Non-HTTP transport or an unauthenticated path: fail closed.
            raise MCPError(types.INVALID_REQUEST, "unauthenticated")
        return principal

    def _track_session(self, ctx: ServerRequestContext[Any, Any], agent_id: str) -> None:
        if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS or ctx.request is None:
            return
        sid = Headers(scope=ctx.request.scope).get("mcp-session-id")
        if not sid:
            return
        with self._lock:
            sessions = self._legacy.setdefault(agent_id, OrderedDict())
            sessions[sid] = ctx.session
            sessions.move_to_end(sid)
            while len(sessions) > MAX_TRACKED_SESSIONS_PER_AGENT:
                sessions.popitem(last=False)

    def _bus(self, agent_id: str) -> tuple[InMemorySubscriptionBus, ListenHandler]:
        with self._lock:
            pair = self._buses.get(agent_id)
            if pair is None:
                bus = InMemorySubscriptionBus()
                pair = (bus, ListenHandler(bus, max_subscriptions=16))
                self._buses[agent_id] = pair
            return pair

    # ------------------------------------------------------------ exposure
    def _cap(self, principal: AgentPrincipal) -> int:
        return max(0, effective_budgets(principal, self._settings).max_tools)

    def _server_cap(self, principal: AgentPrincipal) -> int | None:
        return effective_budgets(principal, self._settings).max_servers

    def visible_tools(
        self, principal: AgentPrincipal
    ) -> list[tuple[MCPToolRecord, MCPServerRecord, bool]]:
        """(tool, server, requires_approval) the agent may see now. Sync (DB)."""
        cap = self._cap(principal)
        server_cap = self._server_cap(principal)
        exposure = self.exposure.get(principal.agent_id)
        with self._factory() as s:
            q = (
                select(MCPToolRecord, MCPServerRecord)
                .join(MCPServerRecord, MCPToolRecord.server_id == MCPServerRecord.id)
                .where(
                    MCPToolRecord.enabled.is_(True),
                    MCPToolRecord.available.is_(True),
                    MCPServerRecord.enabled.is_(True),
                    # Discovery's eligibility rule, same as routing's retriever
                    # (integration gap 6): an offline server's tools are not shown.
                    MCPServerRecord.status != "offline",
                )
            )
            if exposure is not None:
                rows = s.execute(q.where(MCPToolRecord.id.in_(exposure.tool_ids))).all()
                order = {tid: i for i, tid in enumerate(exposure.tool_ids)}
                candidates = sorted(rows, key=lambda r: order[r[0].id])
            else:
                # Deterministic default: most-used first, stable-id tiebreak.
                candidates = list(
                    s.execute(
                        q.order_by(
                            MCPToolRecord.call_count.desc(),
                            MCPServerRecord.name,
                            MCPToolRecord.name,
                        )
                    ).all()
                )
            rules = list(
                s.scalars(select(PolicyRule).where(PolicyRule.agent_id == principal.agent_id)).all()
            )
            out: list[tuple[MCPToolRecord, MCPServerRecord, bool]] = []
            servers_shown: set[str] = set()
            for tool, server in candidates:
                if len(out) >= cap:
                    break
                if (
                    server_cap is not None
                    and server.id not in servers_shown
                    and len(servers_shown) >= server_cap
                ):
                    continue  # distinct-server budget (routing.budgets)
                if self._route_fn is not None and stable_tool_id(server.name, tool.name) in (
                    META_TOOL,
                    FEEDBACK_TOOL,
                ):
                    continue  # never shadow / duplicate the meta tool's name
                # Defense in depth: route results are NEVER shown unfiltered.
                decision = evaluate(principal, server, tool, rules)
                if decision.allow:
                    out.append((tool, server, decision.requires_approval))
                    servers_shown.add(server.id)
            s.expunge_all()  # detach (tools share server objects)
            return out

    def _render(self, rows: list[tuple[MCPToolRecord, MCPServerRecord, bool]]) -> list[types.Tool]:
        tools: list[types.Tool] = []
        for tool, server, approval in rows:
            desc = redact(tool.description or "")
            if approval:
                desc = (desc + " " if desc else "") + "[requires administrator approval]"
            schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
            if schema.get("type") != "object":
                # MCP requires an object schema; the manager still validates
                # against the tool's real schema.
                schema = {"type": "object"}
            tools.append(
                types.Tool(
                    name=stable_tool_id(server.name, tool.name),
                    description=desc,
                    input_schema=schema,
                )
            )
        if self._route_fn is not None:
            tools += [_META_TOOL_DEF, _FEEDBACK_TOOL_DEF]
        return tools

    def _resolve_stable_id(self, name: str) -> str | None:
        """Exact `server.tool` match; ambiguous (dotted names colliding) -> None."""
        with self._factory() as s:
            ids = s.scalars(
                select(MCPToolRecord.id)
                .join(MCPServerRecord, MCPToolRecord.server_id == MCPServerRecord.id)
                .where((MCPServerRecord.name + "." + MCPToolRecord.name) == name)
                .limit(2)
            ).all()
        return ids[0] if len(ids) == 1 else None

    # ------------------------------------------------------------ handlers
    async def _on_list_tools(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        principal = self._principal(ctx)
        self._track_session(ctx, principal.agent_id)
        rows = await anyio.to_thread.run_sync(self.visible_tools, principal)
        tools = self._render(rows)
        if self._skills is not None:
            tools += _SKILL_TOOL_DEFS
        return types.ListToolsResult(tools=tools, cache_scope="private", ttl_ms=0)

    async def _on_call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        principal = self._principal(ctx)
        self._track_session(ctx, principal.agent_id)
        if params.name == META_TOOL and self._route_fn is not None:
            return await self._find_tools(principal, params.arguments)
        if params.name == FEEDBACK_TOOL and self._route_fn is not None:
            return await self._feedback(principal, params.arguments or {})
        if self._skills is not None and params.name in (
            ACTIVATE_SKILL_TOOL,
            READ_SKILL_RESOURCE_TOOL,
        ):
            return await self._skill_tool(principal, params.name, params.arguments or {})
        tool_id = await anyio.to_thread.run_sync(self._resolve_stable_id, params.name)
        # Analytics seam (wave 2): attribute the call to the route that exposed
        # the agent's current tool set. Exposure is per AGENT (all its sessions
        # share it), so this is the agent's last route request_id. Passed only
        # when the manager's execute() declares the optional keyword.
        extra: dict[str, Any] = {}
        exposure = self.exposure.get(principal.agent_id)
        if exposure is not None and self._manager_takes_route_id:
            extra[ROUTE_REQUEST_ID_KWARG] = exposure.request_id
        # Unknown names still go through the manager so the attempt is audited.
        res = await self._manager.execute(principal, tool_id or "", params.arguments or {}, **extra)
        return _to_call_result(res)

    async def _on_listen(
        self, ctx: ServerRequestContext[Any, Any], params: types.SubscriptionsListenRequestParams
    ) -> types.SubscriptionsListenResult:
        principal = self._principal(ctx)
        _, handler = self._bus(principal.agent_id)
        return await handler(ctx, params)

    async def _find_tools(
        self, principal: AgentPrincipal, arguments: dict[str, Any] | None
    ) -> types.CallToolResult:
        query = (arguments or {}).get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LEN:
            return _text(
                f"Refused: 'query' must be a string of 1-{MAX_QUERY_LEN} characters", is_error=True
            )
        if set(arguments or {}) - {"query"}:
            return _text("Refused: unexpected arguments", is_error=True)
        if not self._route_limiter.try_acquire(principal.agent_id):
            return _text("Refused (rate_limited): rate limit exceeded", is_error=True)
        route_fn = self._route_fn
        assert route_fn is not None
        request = RouteRequest(
            query=redact(query),  # model input: secrets never reach the router
            agent_id=principal.agent_id,
            max_tools=self._cap(principal),
            max_servers=self._server_cap(principal),
        )
        try:
            result = await anyio.to_thread.run_sync(route_fn, request)
        except Exception as exc:  # noqa: BLE001 — curated boundary: type name only
            log.warning(
                "gateway.route_failed agent=%s exc_type=%s",
                scrub_log(principal.agent_id),
                type(exc).__name__,
            )
            return _text(
                "Refused: routing is unavailable; the tool list is unchanged", is_error=True
            )
        await self.apply_route(principal.agent_id, result)
        rows = await anyio.to_thread.run_sync(self.visible_tools, principal)
        names = [stable_tool_id(server.name, tool.name) for tool, server, _ in rows]
        if not names:
            return _text("No authorized tools matched; the tool list is now empty.", is_error=False)
        return _text("Tool list updated: " + ", ".join(names), is_error=False)

    # ------------------------------------------------------------ skills
    async def _feedback(
        self, principal: AgentPrincipal, args: dict[str, Any]
    ) -> types.CallToolResult:
        """requestId defaults to the agent's current exposure request id (exposure
        is per agent, shared by its sessions). Errors are curated tool errors."""
        rid = args.get("requestId")
        if rid is None:
            exposure = self.exposure.get(principal.agent_id)
            rid = exposure.request_id if exposure is not None else None
        raw = args.get("items")
        if not isinstance(rid, str) or not rid:
            return _text("No routing decision to give feedback on; pass requestId.", is_error=True)
        if not isinstance(raw, list) or not all(
            isinstance(i, dict) and isinstance(i.get("helpful"), bool) for i in raw
        ):
            return _text("items must be a list of {name, helpful, note?}.", is_error=True)
        # Agents see tools by their stable `server.tool` name; resolve that to the
        # tool id the decision recorded. Anything else (skills, bare names) goes
        # through record_feedback's own name resolution.
        ids = [
            await anyio.to_thread.run_sync(self._resolve_stable_id, str(i.get("name", "")))
            if i.get("kind") != "skill"
            else None
            for i in raw
        ]
        try:
            items = [
                _fb.FeedbackItem(
                    id=tid,
                    helpful=i["helpful"],
                    kind=i.get("kind") if i.get("kind") in ("tool", "skill") else None,
                    name=None if tid else str(i["name"])[:300],
                    note=str(i["note"]) if isinstance(i.get("note"), str) else None,
                )
                for i, tid in zip(raw, ids, strict=True)
            ]
        except KeyError:
            return _text("each item needs name and helpful.", is_error=True)

        def write() -> int:
            with self._factory() as s:
                return _fb.record_feedback(
                    s,
                    request_id=rid,
                    items=items,
                    source="agent",
                    agent_id=principal.agent_id,
                    principal=f"agent:{principal.agent_id}",
                )

        try:
            n = await anyio.to_thread.run_sync(write)
        except _fb.FeedbackError as exc:  # typed, curated messages only
            return _text(f"Feedback not recorded: {exc}", is_error=True)
        return _text(f"Recorded feedback for {n} item(s).", is_error=False)

    def _routed_skills(self, agent_id: str) -> tuple[tuple[str, ...], str | None]:
        exposure = self.exposure.get(agent_id)
        with self._lock:
            if exposure is None:
                # exposure.clear(agent) is the reset: skills go with the tools
                # (fail closed; never serve a stale routed skill set).
                self._skill_ids.pop(agent_id, None)
                return (), None
            ids = self._skill_ids.get(agent_id, ())
        return ids, exposure.request_id

    async def _on_list_prompts(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListPromptsResult:
        principal = self._principal(ctx)
        self._track_session(ctx, principal.agent_id)
        prompts: list[types.Prompt] = []
        if self._skills is not None:
            ids, _ = self._routed_skills(principal.agent_id)
            for sk, src in await anyio.to_thread.run_sync(self._skills.load_routed, ids):
                prompts.append(
                    types.Prompt(
                        name=prompt_name(src.name, sk.name),
                        description=redact(sk.description or ""),
                        arguments=[],
                    )
                )
        return types.ListPromptsResult(prompts=prompts, cache_scope="private", ttl_ms=0)

    async def _on_get_prompt(
        self, ctx: ServerRequestContext[Any, Any], params: types.GetPromptRequestParams
    ) -> types.GetPromptResult:
        principal = self._principal(ctx)
        self._track_session(ctx, principal.agent_id)
        act = await self._activate(principal, params.name)
        return types.GetPromptResult(
            description=act.name,
            messages=[types.PromptMessage(role="user", content=types.TextContent(text=act.body))],
        )

    async def _on_list_resources(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        principal = self._principal(ctx)
        self._track_session(ctx, principal.agent_id)
        out: list[types.Resource] = []
        if self._skills is not None:
            ids, _ = self._routed_skills(principal.agent_id)
            for sk, src in await anyio.to_thread.run_sync(self._skills.load_routed, ids):
                # Malformed entries are skipped (logged) by the shared helper.
                for path, e in manifest_entries(sk).items():
                    size = e.get("size")
                    out.append(
                        types.Resource(
                            name=f"{prompt_name(src.name, sk.name)}/{path}",
                            uri=resource_uri(src.name, sk.name, path),
                            mime_type=resource_mime(path, e.get("kind") == "text"),
                            size=size if isinstance(size, int) else None,
                        )
                    )
        return types.ListResourcesResult(resources=out, cache_scope="private", ttl_ms=0)

    async def _on_read_resource(
        self, ctx: ServerRequestContext[Any, Any], params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        principal = self._principal(ctx)
        self._track_session(ctx, principal.agent_id)
        parsed = _parse_skill_uri(str(params.uri))
        if parsed is None or self._skills is None:
            raise MCPError(types.INVALID_PARAMS, _SKILL_UNKNOWN)
        content = await self._read_skill_resource(principal, parsed[0], parsed[1])
        contents: list[types.TextResourceContents | types.BlobResourceContents]
        if content.text is not None:
            contents = [
                types.TextResourceContents(
                    uri=str(params.uri), mime_type=content.mime_type, text=content.text
                )
            ]
        else:
            contents = [
                types.BlobResourceContents(
                    uri=str(params.uri),
                    mime_type=content.mime_type,
                    blob=base64.b64encode(content.blob or b"").decode("ascii"),
                )
            ]
        return types.ReadResourceResult(contents=contents, cache_scope="private", ttl_ms=0)

    # The ONE path into SkillExposure for prompts/get, resources/read and the
    # meta-tools: visibility (routed ids) -> limiter -> policy -> audit inside it.
    async def _activate(self, principal: AgentPrincipal, name: str) -> Activation:
        if self._skills is None:
            raise MCPError(types.INVALID_PARAMS, _SKILL_UNKNOWN)
        skills = self._skills
        ids, rid = self._routed_skills(principal.agent_id)
        try:
            return await anyio.to_thread.run_sync(
                lambda: skills.activate(principal.agent_id, name, ids, rid)
            )
        except Exception as exc:  # noqa: BLE001 — curated boundary (_skill_mcp_error)
            raise _skill_mcp_error(exc) from None

    async def _read_skill_resource(
        self, principal: AgentPrincipal, name: str, path: str
    ) -> ResourceContent:
        if self._skills is None:
            raise MCPError(types.INVALID_PARAMS, _SKILL_UNKNOWN)
        skills = self._skills
        ids, rid = self._routed_skills(principal.agent_id)
        try:
            return await anyio.to_thread.run_sync(
                lambda: skills.read_resource(principal.agent_id, name, path, ids, rid)
            )
        except Exception as exc:  # noqa: BLE001 — curated boundary (_skill_mcp_error)
            raise _skill_mcp_error(exc) from None

    async def _skill_tool(
        self, principal: AgentPrincipal, tool: str, args: dict[str, Any]
    ) -> types.CallToolResult:
        name, path = args.get("name"), args.get("path")
        allowed = {"name"} if tool == ACTIVATE_SKILL_TOOL else {"name", "path"}
        if not isinstance(name, str) or set(args) - allowed:
            return _text("Refused: invalid arguments", is_error=True)
        try:
            if tool == ACTIVATE_SKILL_TOOL:
                act = await self._activate(principal, name)
                listing = json.dumps(act.resources)
                return _text(f"{act.body}\n\n[skill resources] {listing}", is_error=False)
            if not isinstance(path, str):
                return _text("Refused: invalid arguments", is_error=True)
            content = await self._read_skill_resource(principal, name, path)
        except MCPError as exc:
            return _text(f"Refused: {exc.error.message}", is_error=True)
        if content.text is not None:
            return _text(content.text, is_error=False)
        blob = base64.b64encode(content.blob or b"").decode("ascii")
        return _text(f"[base64 {content.mime_type}] {blob}", is_error=False)

    # ------------------------------------------------------------ re-route
    async def apply_route(self, agent_id: str, result: RouteResult) -> bool:
        """Install an agent's route result; notify its sessions if it changed."""
        tool_ids = [t.tool_id for t in result.tools if t.kind == "tool"]
        skill_ids = tuple(t.tool_id for t in result.tools if t.kind == "skill")
        changed = self.exposure.set(agent_id, tool_ids, result.request_id)
        with self._lock:
            if self._skill_ids.get(agent_id, ()) != skill_ids:
                self._skill_ids[agent_id] = skill_ids
                changed = True
        if changed:
            await self.notify_tools_changed(agent_id)
        return changed

    def apply_route_threadsafe(self, agent_id: str, result: RouteResult) -> bool:
        """For sync callers on an anyio worker thread (e.g. the sync REST
        /route endpoint): `gateway.apply_route_threadsafe(agent_id, result)`."""
        return anyio.from_thread.run(self.apply_route, agent_id, result)

    async def notify_tools_changed(self, agent_id: str) -> None:
        bus, _ = self._bus(agent_id)
        await bus.publish(ToolsListChanged())
        if self._skills is not None:
            await bus.publish(PromptsListChanged())
            await bus.publish(ResourcesListChanged())
        with self._lock:
            sessions = list(self._legacy.get(agent_id, {}).items())
        dead: list[str] = []

        async def one(sid: str, session: ServerSession) -> None:
            with anyio.move_on_after(NOTIFY_TIMEOUT_S) as scope:
                try:
                    await session.send_tool_list_changed()
                    if self._skills is not None:
                        await session.send_prompt_list_changed()
                        await session.send_resource_list_changed()
                except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                    dead.append(sid)
            if scope.cancelled_caught:
                dead.append(sid)

        # Concurrently: N stalled sessions cost ~NOTIFY_TIMEOUT_S total, not N x
        # (a /route caller blocks on this through apply_route_threadsafe).
        async with anyio.create_task_group() as tg:
            for sid, session in sessions:
                tg.start_soon(one, sid, session)
        if dead:
            with self._lock:
                live = self._legacy.get(agent_id)
                for sid in dead:
                    if live is not None:
                        live.pop(sid, None)


def build_gateway(
    app: FastAPI,
    *,
    manager: ExecutionManager,
    route_fn: RouteFn | None,
    transport_security: TransportSecuritySettings | None = None,
    host: str = "127.0.0.1",
    skills: SkillExposure | None = None,
) -> GatewayServer:
    """Integrator helper: build from app.state and mount at /mcp."""
    gw = GatewayServer(
        session_factory=app.state.session_factory,
        security=app.state.security,
        settings=app.state.settings,
        manager=manager,
        route_fn=route_fn,
        transport_security=transport_security,
        host=host,
        skills=skills,
    )
    gw.mount(app)
    app.state.gateway = gw
    return gw


__all__: list[str] = ["FEEDBACK_TOOL", "MCP_PATH", "META_TOOL", "GatewayServer", "build_gateway"]
