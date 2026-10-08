"""App factory: the one place every workstream's seam is wired.

Order matters:
  schema (init_db + registry/routing indexes) -> security (tables, env
  principals, app.state.security) -> inference engine (constructed, loaded in
  the lifespan) -> management routers (admin-gated) -> routing (policy-backed
  scope, decision deadline) -> execution manager (connector invoker) ->
  policy/approval routes -> MCP gateway at /mcp (wraps the lifespan).

Wave 2 (budgets/cache/lifecycle): DiscoveryService carries the post-sync
classify+embed hook (mcprouter.lifecycle); the lifespan starts the SyncLoop
when MCPR_SYNC_ENABLED and stops it on shutdown, before the engine unloads.

Wave 2 (integration): POST /api/v1/tools/{id}/execute (routes_execute) runs
through the one ExecutionManager; analytics routes + metrics collector are
installed by install_analytics.

Run ONE uvicorn worker: the rate limiter, exposure sets and MCP notification
routing are in-process state (docs/INTEGRATION_NOTES-gateway.md).
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator, Mapping

import anyio.to_thread
from fastapi import FastAPI
from prometheus_client import make_asgi_app

from mcprouter.api import routes_dedup, routes_tools
from mcprouter.api.deps_auth import configure_security
from mcprouter.api.routes_analytics import install_analytics
from mcprouter.api.routes_execute import router as execute_router
from mcprouter.api.routes_executions import router as executions_router
from mcprouter.api.routes_models import router as models_router
from mcprouter.api.routes_policy import router as policy_router
from mcprouter.api.routes_route import install_routing
from mcprouter.api.routes_servers import router as servers_router
from mcprouter.db import init_db, make_engine, make_session_factory
from mcprouter.discovery import DiscoveryService, SyncLoop
from mcprouter.execution.invoker import ConnectorToolInvoker
from mcprouter.execution.manager import ExecutionManager
from mcprouter.gateway.server import build_gateway
from mcprouter.inference.adapters import DeadlineDecisionModel, EngineEmbedder
from mcprouter.inference.engine import InferenceEngine
from mcprouter.interfaces import RouteRequest, RouteResult
from mcprouter.lifecycle import make_post_sync_hook
from mcprouter.policy.scope import policy_scope_resolver
from mcprouter.registry.schema import init_registry
from mcprouter.routing.pipeline import RoutePipeline
from mcprouter.routing.retriever import HybridRetriever
from mcprouter.settings import Settings

log = logging.getLogger(__name__)

# Third-party clients that log full upstream request URLs at INFO (a token in
# a query string would land in logs). Raised to WARNING at startup.
_NOISY_CLIENT_LOGGERS = ("httpx", "httpx2", "httpcore", "mcp.client")


def _quiet_client_loggers() -> None:
    for name in _NOISY_CLIENT_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level == logging.NOTSET:  # respect an operator's explicit level
            logger.setLevel(logging.WARNING)


def create_app(
    settings: Settings | None = None, *, env: Mapping[str, str] | None = None
) -> FastAPI:
    """`env` carries secrets that never enter Settings (MCPR_ADMIN_TOKEN);
    defaults to os.environ. Tests pass an explicit mapping (pure)."""
    settings = settings or Settings.from_env()
    env = os.environ if env is None else env
    _quiet_client_loggers()

    inference = InferenceEngine(
        settings,
        idle_unload_s=settings.idle_unload_s,
        embed_batch_size=settings.embed_batch_size,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # SPEC §7 "load once at startup" — off the event loop (Laya: seconds).
        await anyio.to_thread.run_sync(inference.load)
        loop: SyncLoop | None = None
        try:
            if settings.sync_enabled:  # MCPR_SYNC_ENABLED (integration gap 4)
                loop = SyncLoop(_app.state.discovery)
                await loop.start()
                _app.state.sync_loop = loop
            yield
        finally:
            if loop is not None:
                await loop.stop()
            await anyio.to_thread.run_sync(inference.unload)

    app = FastAPI(title="MCP Router", version="0.1.0", docs_url="/docs", lifespan=lifespan)
    engine = make_engine(settings)
    init_db(engine)  # Base + approval_requests + eval_results (Alembic deferred)
    init_registry(engine)  # FTS + dedup-pair indexes (idempotent)
    factory = make_session_factory(engine)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory

    # Auth: dev mode only when agent keys, admin token AND principals are all
    # absent — deps_auth logs the per-request `auth.dev_mode` warning.
    security = configure_security(app, env)
    if security.dev_mode_possible:
        log.warning(
            "No MCPR_AGENT_KEYS and no MCPR_ADMIN_TOKEN: auth is DISABLED while no agent "
            "principals exist (dev mode; admin API open). Never expose beyond localhost."
        )

    app.state.inference_engine = inference
    embedder = EngineEmbedder(inference)
    # Post-sync lifecycle (integration gap 1): classify + embed after any sync
    # that changed the catalog — manual refresh and the SyncLoop alike.
    app.state.discovery = DiscoveryService(
        factory,
        post_sync=make_post_sync_hook(
            factory, embedder, embed_batch_size=settings.embed_batch_size
        ),
    )
    app.state.sync_loop = None  # started in the lifespan when MCPR_SYNC_ENABLED

    # Management API — all admin-gated by the gateway's require_admin
    # (servers/models/executions at the router; tools/dedup via the registry
    # wrapper; route/evaluate and policy routes per endpoint).
    app.include_router(servers_router)
    app.include_router(routes_tools.router)
    app.include_router(routes_dedup.router)
    app.include_router(models_router)
    app.include_router(executions_router)

    # Routing: policy-backed scope (ONE policy implementation) + model deadline.
    scope_resolver = policy_scope_resolver(factory, security)
    pipeline = RoutePipeline(
        factory,
        HybridRetriever(factory, embedder),
        DeadlineDecisionModel.for_engine(inference, settings.decision_timeout_s),
        settings,
    )
    install_routing(app, pipeline, scope_resolver=scope_resolver)

    def route_fn(request: RouteRequest) -> RouteResult:
        # The gateway redacts the query before calling this (find_tools).
        return pipeline.route(request, scope_resolver(request.agent_id))

    manager = ExecutionManager.from_settings(settings, factory, ConnectorToolInvoker(factory))
    app.state.execution_manager = manager
    app.include_router(policy_router)
    # REST execution (playground): a thin route over the SAME manager.
    app.include_router(execute_router)
    # /api/v1/analytics/* (admin) + the Prometheus funnel collector.
    install_analytics(app)
    build_gateway(app, manager=manager, route_fn=route_fn)  # /mcp; wraps the lifespan

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        from sqlalchemy import text

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    app.mount("/metrics", make_asgi_app())
    return app
