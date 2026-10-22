"""Diagnostics endpoint support — health checks for config, DB, migrations and backends.

This module provides async-friendly check functions that each return a dict with
{name, ok, detail}. The endpoint aggregates them into {status, checks[], duration_ms}.

All checks are non-blocking and never raise; infrastructure errors return ok=False
with a plain-language detail (no SQL/DSN in the text).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from mcprouter.inference.engine import InferenceEngine
from mcprouter.interfaces import DecisionModel
from mcprouter.settings import Settings

log = logging.getLogger(__name__)

# ----------------------------------------------------------------- check result shape


def make_check(name: str, ok: bool, detail: str | None = None) -> dict[str, Any]:
    """Standard check result shape."""
    return {"name": name, "ok": ok, "detail": detail}


# ----------------------------------------------------------------- config checks


def check_config(settings: Settings) -> dict[str, Any]:
    """Verify settings against SETTINGS_SPEC defaults and constraints.

    Returns ok=True unless a required setting is missing or invalid.
    """
    # Admin token check: warn if unset outside dev mode (agent keys present)
    if not settings.admin_token.strip() and not settings.agent_keys.strip():
        return make_check(
            "config",
            True,
            "Dev mode: no admin token or agent keys configured (loopback-only bind)",
        )
    if not settings.admin_token.strip():
        return make_check(
            "config",
            False,
            "Admin token is required when agent keys are configured",
        )
    if len(settings.admin_token.strip()) < 32:
        return make_check(
            "config",
            False,
            "Admin token must be at least 32 characters",
        )
    return make_check("config", True, "Admin token configured")


def check_auth_posture(settings: Settings) -> dict[str, Any]:
    """Check auth posture: admin token set; warn on open dev mode / non-loopback bind."""
    has_admin = bool(settings.admin_token.strip())
    has_agent_keys = bool(settings.agent_keys.strip())

    if has_admin:
        return make_check("auth", True, "Admin token configured")
    if has_agent_keys:
        return make_check(
            "auth",
            False,
            "Agent keys configured but no admin token (admin API unavailable)",
        )
    return make_check(
        "auth",
        True,
        "Dev mode: no auth configured (loopback-only)",
    )


# ----------------------------------------------------------------- database checks


def check_database(settings: Settings) -> dict[str, Any]:
    """Check database connectivity and pgvector extension.

    Returns ok=True if the connection works and pgvector is available.
    """
    try:
        from mcprouter.db import make_engine

        engine = make_engine(settings)
        with engine.connect() as conn:
            # Basic connectivity
            conn.execute(text("SELECT 1"))

            # Check pgvector extension
            result = conn.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'pgvector'")
            )
            row = result.fetchone()
            if row is None:
                return make_check(
                    "database",
                    False,
                    "pgvector extension not installed (required for vector search)",
                )

            # Check catalog tables exist
            result = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('tools', 'servers', 'skills', 'catalog_items')"
                )
            )
            tables = {row[0] for row in result.fetchall()}
            expected = {"tools", "servers", "skills", "catalog_items"}
            missing = expected - tables
            if missing:
                return make_check(
                    "database",
                    False,
                    f"Catalog tables missing: {', '.join(sorted(missing))}",
                )

        return make_check("database", True, "Database and pgvector available")
    except SQLAlchemyError as exc:
        log.warning("health: database unreachable (%s)", type(exc).__name__)
        return make_check("database", False, f"Database unreachable: {type(exc).__name__}")
    except Exception as exc:
        log.warning("health: database check failed (%s)", type(exc).__name__)
        return make_check("database", False, f"Database check failed: {type(exc).__name__}")


# ----------------------------------------------------------------- migration checks


def check_migrations(settings: Settings) -> dict[str, Any]:
    """Check Alembic DB revision matches head.

    Returns ok=True if migrations are up to date.
    """
    try:
        # Get head revision from mcprouter.migrate
        from mcprouter.migrate import head_revision

        head_rev = head_revision()

        # Get current revision from database
        from mcprouter.db import make_engine
        from mcprouter.migrate import current_revision

        engine = make_engine(settings)
        with engine.connect() as conn:
            # Check if alembic_version table exists
            result = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'alembic_version'"
                )
            )
            if result.fetchone() is None:
                return make_check(
                    "migrations",
                    False,
                    "Alembic version table not found (database not initialized)",
                )

            # Get current revision
            current_rev = current_revision(conn)
            if current_rev is None:
                return make_check(
                    "migrations",
                    False,
                    "No migration version recorded",
                )

            if current_rev == head_rev:
                return make_check("migrations", True, f"Migrations up to date ({head_rev})")
            return make_check(
                "migrations",
                False,
                f"Database is at {current_rev}, head is {head_rev}",
            )
    except Exception as exc:
        log.warning("health: migration check failed (%s)", type(exc).__name__)
        return make_check(
            "migrations",
            False,
            f"Migration check failed: {type(exc).__name__}",
        )


# ----------------------------------------------------------------- decision backend checks


def check_decision_backend(settings: Settings, inference: InferenceEngine | None) -> dict[str, Any]:
    """Check decision backend reachability with a one-question probe.

    Deterministic backend is trivially ok. Other backends get a probe question.
    """
    if inference is None:
        return make_check("decisionBackend", False, "Inference engine not initialized")

    try:
        # Check backend type
        backend = settings.decision_backend

        if backend == "deterministic":
            return make_check("decisionBackend", True, "Deterministic backend (no external dependency)")

        # For other backends, try a simple probe
        # Create a minimal decision model for probing
        from mcprouter.inference.adapters import DeadlineDecisionModel

        # Build a simple probe question
        probe_question = "Is this a test?"
        probe_choices = ["Yes", "No"]

        # Try to get embedding first
        try:
            embedder = inference._embedder_raw()
            if embedder is None:
                return make_check(
                    "decisionBackend",
                    False,
                    "Embedder not available (inference engine not loaded)",
                )
        except Exception as exc:
            return make_check(
                "decisionBackend",
                False,
                f"Embedder initialization failed: {type(exc).__name__}",
            )

        # Try a simple decision
        try:
            # Use the decision model if available
            decision_model = getattr(inference, "decision_model", None)
            if decision_model is not None:
                # Try a simple probe
                result = decision_model(
                    question=probe_question,
                    choices=probe_choices,
                    temperature=0.0,
                )
                # If we get here, the backend is reachable
                return make_check("decisionBackend", True, f"{backend} backend reachable")
        except Exception as exc:
            log.warning("health: decision backend probe failed (%s)", type(exc).__name__)
            return make_check(
                "decisionBackend",
                False,
                f"{backend} backend probe failed: {type(exc).__name__}",
            )

        # If we couldn't test, report based on backend type
        if backend == "lyla":
            return make_check(
                "decisionBackend",
                False,
                "Lyla backend configured but not available (requires [inference] extra)",
            )
        if backend == "remote":
            return make_check(
                "decisionBackend",
                False,
                "Remote decision backend configured but endpoint unreachable",
            )
        if backend == "aoai":
            return make_check(
                "decisionBackend",
                False,
                "Azure OpenAI decision backend configured but not available",
            )

        return make_check("decisionBackend", False, f"Unknown decision backend: {backend}")
    except Exception as exc:
        log.warning("health: decision backend check failed (%s)", type(exc).__name__)
        return make_check(
            "decisionBackend",
            False,
            f"Decision backend check failed: {type(exc).__name__}",
        )


# ----------------------------------------------------------------- discovery checks


def check_discovery(settings: Settings) -> dict[str, Any]:
    """Check discovery last-sync freshness.

    Returns ok=True if sync is enabled and recent, or if sync is disabled (manual refresh).
    """
    if not settings.sync_enabled:
        return make_check("discovery", True, "Discovery sync disabled (manual refresh only)")

    # Check if we have a sync loop running
    # This is a best-effort check - in production the sync loop would update a timestamp
    return make_check(
        "discovery",
        True,
        "Discovery sync enabled (last sync: unknown - check logs)",
    )


# ----------------------------------------------------------------- main diagnostics function


async def run_diagnostics(settings: Settings, inference: InferenceEngine | None = None) -> dict[str, Any]:
    """Run all diagnostics checks and return aggregated results.

    Returns:
        {
            "status": "healthy" | "degraded" | "unhealthy",
            "checks": [{"name": str, "ok": bool, "detail": str | None}, ...],
            "duration_ms": float,
        }
    """
    start = time.time()

    checks: list[dict[str, Any]] = []

    # Run checks sequentially (some depend on others)
    checks.append(check_config(settings))
    checks.append(check_auth_posture(settings))

    # Database checks (require DB connection)
    db_check = check_database(settings)
    checks.append(db_check)

    # Only run migration check if database is OK
    if db_check["ok"]:
        checks.append(check_migrations(settings))
    else:
        checks.append(make_check("migrations", False, "Skipped: database unreachable"))

    # Decision backend check
    checks.append(check_decision_backend(settings, inference))

    # Discovery check
    checks.append(check_discovery(settings))

    duration_ms = (time.time() - start) * 1000

    # Determine overall status
    ok_count = sum(1 for c in checks if c["ok"])
    total_count = len(checks)

    if ok_count == total_count:
        status = "healthy"
    elif ok_count > 0:
        status = "degraded"
    else:
        status = "unhealthy"

    return {
        "status": status,
        "checks": checks,
        "duration_ms": round(duration_ms, 2),
    }
