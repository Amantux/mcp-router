"""Shared in-process edge app for the decision-edge hardening tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcprouter.api.app import create_app
from tests.support.edge import KEY, _drop_principal, _settings
from tests.support.serve import run_app

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


@contextmanager
def served_edge(**kw: object) -> Iterator[tuple[FastAPI, str]]:
    """The edge app served over a real socket (OS-assigned port); yields
    ``(app, base_url)``. Use when the request must not go through
    Starlette's TestClient (e.g. raw non-ASCII header bytes)."""
    s = _settings(**{"decision_backend": "deterministic", "embedding_backend": "hash", **kw})
    app = create_app(s, env={"MCPR_AGENT_KEYS": f"edgebot:{KEY}"})
    port, stop = run_app(app)
    try:
        yield app, f"http://127.0.0.1:{port}"
    finally:
        stop()
        _drop_principal(s)
