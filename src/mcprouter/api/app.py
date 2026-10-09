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
routing are in-process state (docs/history/INTEGRATION_NOTES-gateway.md).
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime

import anyio.to_thread
from fastapi import FastAPI
from prometheus_client import make_asgi_app

from mcprouter import __version__
from mcprouter.analytics.scheduler import RollupLoop
from mcprouter.api import hardening, routes_dedup, routes_skill_sources, routes_skills, routes_tools
from mcprouter.api.body_limit import BodySizeLimitMiddleware
from mcprouter.api.deps_auth import configure_security
from mcprouter.api.errors import install_error_handlers
from mcprouter.api.routes_analytics import install_analytics
from mcprouter.api.routes_decision import router as decision_router
from mcprouter.api.routes_execute import router as execute_router
from mcprouter.api.routes_executions import router as executions_router
from mcprouter.api.routes_models import router as models_router
from mcprouter.api.routes_policy import router as policy_router
from mcprouter.api.routes_route import install_routing
from mcprouter.api.routes_servers import router as servers_router
from mcprouter.api.routes_setup import router as setup_router
from mcprouter.api.static import mount_ui
from mcprouter.db import init_db, make_engine, make_session_factory
from mcprouter.discovery import DiscoveryService, SyncLoop
from mcprouter.execution.invoker import ConnectorToolInvoker
from mcprouter.execution.manager import ExecutionManager
from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.gateway.server import build_gateway, gateway_transport_security
from mcprouter.gateway.skills import SkillExposure
from mcprouter.inference.adapters import DeadlineDecisionModel, EngineEmbedder
from mcprouter.inference.engine import InferenceEngine
from mcprouter.interfaces import RouteRequest, RouteResult
from mcprouter.lifecycle import (
    make_post_sync_hook,
    run_due_skill_syncs,
    run_skill_post_sync,
)
from mcprouter.limits import make_limiters
from mcprouter.logging import configure_logging
from mcprouter.policy.scope import policy_scope_resolver
from mcprouter.policy.skill_bridge import EngineSkillPolicy
from mcprouter.registry.schema import init_registry
from mcprouter.routing.pipeline import RoutePipeline
from mcprouter.routing.retriever import HybridRetriever, ensure_skill_keyword_index
from mcprouter.settings import Settings
from mcprouter.singleton import claim_loop_owner, release_loop_owner

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
    configure_logging(settings)
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
        rollups: RollupLoop | None = None
        try:
            # Only the loop owner runs background loops (E6; always True for now).
            owner = claim_loop_owner(_app.state.engine)
            if owner and settings.sync_enabled:  # MCPR_SYNC_ENABLED (integration gap 4)
                attempts: dict[str, datetime] = {}

                async def _skill_tick() -> object:
                    return await anyio.to_thread.run_sync(
                        run_due_skill_syncs,
                        _app.state.session_factory,
                        settings,
                        _app.state.skill_post_sync,
                        datetime.now(UTC),
                        attempts,
                    )

                loop = SyncLoop(_app.state.discovery, skill_tick=_skill_tick)
                await loop.start()
                _app.state.sync_loop = loop
            if owner and settings.analytics_rollup_enabled:  # MCPR_ANALYTICS_ROLLUP_ENABLED
                rollups = RollupLoop(_app.state.session_factory)
                await rollups.start()
                _app.state.rollup_loop = rollups
            yield
        finally:
            # Background loops stop BEFORE the inference engine unloads.
            if rollups is not None:
                await rollups.stop()
            if loop is not None:
                await loop.stop()
            release_loop_owner(_app.state.engine)  # E6 P-602: next process may own
            await anyio.to_thread.run_sync(inference.unload)

    app = FastAPI(title="MCP Router", version=__version__, docs_url="/docs", lifespan=lifespan)
    install_error_handlers(app)  # app-wide handlers (E2); /route 422 stays in routes_route
    engine = make_engine(settings)
    init_db(engine)  # Base + approval_requests + eval_results (Alembic deferred)
    init_registry(engine)  # FTS + dedup-pair indexes (idempotent)
    ensure_skill_keyword_index(engine)  # skills FTS leg (idempotent)
    factory = make_session_factory(engine)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.limiters = make_limiters(settings)  # E6 fills the registry

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

    def skill_post_sync(source_id: str, before: dict[str, str]) -> None:
        run_skill_post_sync(
            factory, embedder, source_id, before, embed_batch_size=settings.embed_batch_size
        )

    app.state.skill_post_sync = skill_post_sync
    app.state.sync_loop = None  # started in the lifespan when MCPR_SYNC_ENABLED
    app.state.rollup_loop = None  # started in the lifespan when MCPR_ANALYTICS_ROLLUP_ENABLED

    # Management API — all admin-gated by the gateway's require_admin
    # (servers/models/executions at the router; tools/dedup via the registry
    # wrapper; route/evaluate and policy routes per endpoint).
    app.include_router(servers_router)
    app.include_router(routes_tools.router)
    app.include_router(routes_dedup.router)
    app.include_router(models_router)
    app.include_router(decision_router)
    app.include_router(executions_router)

    # Routing: policy-backed scope (ONE policy implementation) + model deadline.
    scope_resolver = policy_scope_resolver(factory, security)
    # ONE deadline model per app: its in-flight cap bounds the worker threads a
    # wedged backend can strand, across routing AND the decision edge.
    decision_model = DeadlineDecisionModel.for_engine(inference, settings.decision_timeout_s)
    app.state.decision_model = decision_model
    pipeline = RoutePipeline(factory, HybridRetriever(factory, embedder), decision_model, settings)
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
    # Skills: S3 exposure gated by the ONE kind-aware policy engine (bridge).
    skill_exposure = SkillExposure(
        factory,
        manager,
        EngineSkillPolicy(factory),
        SlidingWindowLimiter(settings.rate_limit_per_agent_per_min, 60.0),
        cache_dir=settings.skills_cache_dir,
        body_max_bytes=settings.skill_body_max_bytes,
        resource_max_bytes=settings.skill_resource_max_bytes,
    )
    app.state.skill_exposure = skill_exposure
    app.include_router(routes_skill_sources.router)
    app.include_router(setup_router)  # wave-5 first-run wizard (admin-gated)
    app.include_router(routes_skills.agent_router)  # BEFORE router: /skills/bundle
    app.include_router(routes_skills.router)
    build_gateway(
        app,
        manager=manager,
        route_fn=route_fn,
        skills=skill_exposure,
        transport_security=gateway_transport_security(settings.allowed_hosts),
    )  # /mcp; wraps the lifespan

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        from sqlalchemy import text

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    app.mount("/metrics", make_asgi_app())
    # Dashboard + SPA fallback: registered LAST so API, /mcp and /metrics win.
    mount_ui(app, settings.ui_dist)
    # Outermost: refuse oversized bodies before routing, auth or parsing.
    app.add_middleware(BodySizeLimitMiddleware)
    # LAST = outermost middleware slot, reserved for HostGuard et al. (E1, D2).
    hardening.install(app, settings)
    return app
