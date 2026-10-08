"""Engine wiring for remote/aoai backends + health leakage."""

from __future__ import annotations

import dataclasses
import json

import pytest
from fastapi.testclient import TestClient

from mcprouter.api.app import create_app
from mcprouter.inference.engine import DECISION_BACKENDS, EMBEDDING_BACKENDS, InferenceEngine
from mcprouter.inference.validation import ValidatedDecisionModel
from mcprouter.settings import Settings

KEY = "sk-a"  # short, substring-prone: appears inside many words if leaked naively
URL = "https://edge.example.com:9443/secret/path/v1/decisions?token=qq"


def _s(**kw: object) -> Settings:
    return dataclasses.replace(Settings.from_env(), **kw)  # type: ignore[arg-type]


def test_backend_tuples() -> None:
    assert {"remote", "aoai"} <= set(DECISION_BACKENDS)
    assert "aoai" in EMBEDDING_BACKENDS


def test_remote_builds_wrapped_and_named() -> None:
    eng = InferenceEngine(
        _s(decision_backend="remote", decision_endpoint=URL, decision_api_key=KEY)
    )
    eng.load()
    assert isinstance(eng._decider, ValidatedDecisionModel)
    h = eng.health()
    assert h["decision"]["backend"].startswith("remote:")
    assert h["decision"]["fallbackUsed"] is False


def test_remote_without_key_degrades_with_curated_reason() -> None:
    eng = InferenceEngine(_s(decision_backend="remote", decision_endpoint=URL, decision_api_key=""))
    eng.load()
    h = eng.health()["decision"]
    assert h["fallbackUsed"] is True and "MCPR_DECISION_API_KEY is not set" in h["fallbackReason"]


def test_aoai_misconfig_degrades_without_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_AOAI_ENDPOINT", "http://169.254.169.254/latest")
    monkeypatch.setenv("MCPR_AOAI_API_KEY", "super-secret-aoai")
    eng = InferenceEngine(_s(decision_backend="aoai", embedding_backend="aoai"))
    eng.load()
    h = json.dumps(eng.health())
    assert '"fallbackUsed": true' in h
    assert "super-secret-aoai" not in h and "169.254" not in h and "Traceback" not in h


def test_health_exposes_host_only_never_key_or_url() -> None:
    s = _s(decision_backend="remote", decision_endpoint=URL, decision_api_key=KEY)
    with TestClient(create_app(s, env={"MCPR_ADMIN_TOKEN": "admintok-123456789"})) as c:
        r = c.get("/api/v1/models/health", headers={"Authorization": "Bearer admintok-123456789"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decisionBackend"] == {
        "kind": "remote",
        "model": "typesafe/jev",
        "endpointHost": "edge.example.com",
    }
    assert body["embeddingBackend"]["kind"] == s.embedding_backend
    raw = r.text
    for leak in ("sk-a", "/secret/path", "token=qq", "9443", URL):
        assert leak not in raw, leak
