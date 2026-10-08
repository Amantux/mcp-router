"""Shared in-process edge app for the decision-edge hardening tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcprouter.api.app import create_app
from tests.test_edge_roundtrip import KEY, _drop_principal, _settings

AUTH = {"Authorization": f"Bearer {KEY}"}
PATH = "/api/v1/decision/systemone"


@contextmanager
def edge_client(**kw: object) -> Iterator[tuple[FastAPI, TestClient]]:
    s = _settings(**{"decision_backend": "deterministic", "embedding_backend": "hash", **kw})
    app = create_app(s, env={"MCPR_AGENT_KEYS": f"edgebot:{KEY}"})
    try:
        with TestClient(app) as client:
            yield app, client
    finally:
        _drop_principal(s)
