"""GET /api/v1/models/health."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcprouter.api.deps_auth import require_admin
from mcprouter.api.routes_models import router
from mcprouter.inference.engine import InferenceEngine
from mcprouter.inference.errors import ModelUnavailableError
from mcprouter.interfaces import EmbeddingBackend
from mcprouter.settings import Settings


def _app(engine: InferenceEngine | None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    # Admin auth is covered in tests/test_integration_auth.py; these test the probe.
    app.dependency_overrides[require_admin] = lambda: None
    if engine is not None:
        app.state.inference_engine = engine
    return TestClient(app)


def test_health_before_load_does_not_load_models() -> None:
    eng = InferenceEngine(Settings())
    body = _app(eng).get("/api/v1/models/health").json()
    assert body["loaded"] is False
    assert body["embedding"]["backend"] is None
    assert eng.loaded is False


def test_health_after_load_reports_backends_device_mode_and_stats() -> None:
    eng = InferenceEngine(Settings(operating_mode="performance"))
    eng.load()
    eng.embed(["warm"])
    r = _app(eng).get("/api/v1/models/health")
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "ok" and b["loaded"] is True
    assert b["mode"] == "performance" and b["device"] == "cpu"
    assert b["embedding"]["backend"] == "hash-v1"
    assert b["decision"]["backend"] == "deterministic-v1"
    assert b["concurrency"]["capacity"] == 4 and b["concurrency"]["acquiredTotal"] == 1
    assert "rssBytes" in b["memory"]
    assert set(b["versions"]) >= {"torch", "laya"}
    assert b["idleUnload"]["enabled"] is False


def test_health_reports_degradation_with_curated_reason() -> None:
    def unavailable(settings: Settings, device: str) -> EmbeddingBackend:
        raise ModelUnavailableError(
            "embedding model unavailable: the [inference] extra is not installed"
        )

    eng = InferenceEngine(Settings(embedding_backend="bge"), embedding_loader=unavailable)
    eng.load()
    b = _app(eng).get("/api/v1/models/health").json()
    assert b["status"] == "degraded"
    assert b["embedding"]["fallbackUsed"] is True
    assert b["embedding"]["fallbackReason"].startswith("embedding model unavailable")


def test_health_leaks_no_secrets_or_paths() -> None:
    eng = InferenceEngine(
        Settings(
            agent_keys="ops:topsecretkey",
            database_url="postgresql+psycopg://u:dbpw@db/x",
            models_cache_dir="/srv/private/models",
        )
    )
    eng.load()
    text = _app(eng).get("/api/v1/models/health").text
    for needle in ("topsecretkey", "dbpw", "/srv/private", "postgresql", "models-cache"):
        assert needle not in text


def test_missing_engine_is_a_curated_503() -> None:
    r = _app(None).get("/api/v1/models/health")
    assert r.status_code == 503
    assert r.json() == {"detail": "inference engine is not configured"}


def test_openapi_has_typed_response_schema() -> None:
    schema = _app(InferenceEngine(Settings())).get("/openapi.json").json()
    ok = schema["paths"]["/api/v1/models/health"]["get"]["responses"]["200"]
    assert ok["content"]["application/json"]["schema"]["$ref"].endswith("/ModelsHealth")
