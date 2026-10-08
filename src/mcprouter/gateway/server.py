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
  top-N most-used tools), RE-FILTERED through policy, capped at
  min(principal.max_tools, settings.max_exposed_tools). Stable names
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

import contextlib
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator
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
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.authentication import AuthCredentials
from starlette.datastructures import Headers
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from mcprouter.api.deps_auth import AuthenticationError, SecurityConfig, hash_key, resolve_principal
from mcprouter.execution.manager import ExecutionManager, ExecutionResult, stable_tool_id
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.execution.redaction import redact, scrub_log
from mcprouter.gateway.exposure import ExposureStore
from mcprouter.interfaces import RouteFn, RouteRequest, RouteResult
from mcprouter.models import AgentPrincipal, MCPServerRecord, MCPToolRecord, PolicyRule
from mcprouter.policy.engine import evaluate
from mcprouter.settings import Settings

log = logging.getLogger(__name__)

MCP_PATH = "/mcp"
META_TOOL = "router.find_tools"
PRINCIPAL_SCOPE_KEY = "mcprouter.principal"
MAX_QUERY_LEN = 2000
MAX_TRACKED_SESSIONS_PER_AGENT = 32
NOTIFY_TIMEOUT_S = 2.0

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
            notification_options or NotificationOptions(tools_changed=True),
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
    ) -> None:
        self._factory = session_factory
        self._security = security
        self._settings = settings
        self._manager = manager
        self._route_fn = route_fn
        self.exposure = ExposureStore()
        self._route_limiter = SlidingWindowLimiter(settings.rate_limit_per_agent_per_min)
        self._buses: dict[str, tuple[InMemorySubscriptionBus, ListenHandler]] = {}
        self._legacy: dict[str, OrderedDict[str, ServerSession]] = {}
        self._lock = threading.Lock()
        self.server = _RouterMCPServer(
            "mcp-router",
            version="0.1.0",
            instructions=(
                "Tools are exposed per agent and change as you work. Call "
                f"{META_TOOL} with a task description to get relevant tools."
            ),
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
            on_subscriptions_listen=self._on_listen,
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
        return max(0, min(principal.max_tools, self._settings.max_exposed_tools))

    def visible_tools(
        self, principal: AgentPrincipal
    ) -> list[tuple[MCPToolRecord, MCPServerRecord, bool]]:
        """(tool, server, requires_approval) the agent may see now. Sync (DB)."""
        cap = self._cap(principal)
        exposure = self.exposure.get(principal.agent_id)
        with self._factory() as s:
            q = (
                select(MCPToolRecord, MCPServerRecord)
                .join(MCPServerRecord, MCPToolRecord.server_id == MCPServerRecord.id)
                .where(
                    MCPToolRecord.enabled.is_(True),
                    MCPToolRecord.available.is_(True),
                    MCPServerRecord.enabled.is_(True),
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
            for tool, server in candidates:
                if len(out) >= cap:
                    break
                if (
                    self._route_fn is not None
                    and stable_tool_id(server.name, tool.name) == META_TOOL
                ):
                    continue  # never shadow / duplicate the meta tool's name
                # Defense in depth: route results are NEVER shown unfiltered.
                decision = evaluate(principal, server, tool, rules)
                if decision.allow:
                    out.append((tool, server, decision.requires_approval))
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
            tools.append(_META_TOOL_DEF)
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
        return types.ListToolsResult(tools=self._render(rows), cache_scope="private", ttl_ms=0)

    async def _on_call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        principal = self._principal(ctx)
        self._track_session(ctx, principal.agent_id)
        if params.name == META_TOOL and self._route_fn is not None:
            return await self._find_tools(principal, params.arguments)
        tool_id = await anyio.to_thread.run_sync(self._resolve_stable_id, params.name)
        # Unknown names still go through the manager so the attempt is audited.
        res = await self._manager.execute(principal, tool_id or "", params.arguments or {})
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

    # ------------------------------------------------------------ re-route
    async def apply_route(self, agent_id: str, result: RouteResult) -> bool:
        """Install an agent's route result; notify its sessions if it changed."""
        changed = self.exposure.set(agent_id, [t.tool_id for t in result.tools], result.request_id)
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
        with self._lock:
            sessions = list(self._legacy.get(agent_id, {}).items())
        dead: list[str] = []

        async def one(sid: str, session: ServerSession) -> None:
            with anyio.move_on_after(NOTIFY_TIMEOUT_S) as scope:
                try:
                    await session.send_tool_list_changed()
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
    )
    gw.mount(app)
    app.state.gateway = gw
    return gw


__all__: list[str] = ["MCP_PATH", "META_TOOL", "GatewayServer", "build_gateway"]
