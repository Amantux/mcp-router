"""Decision edge: per-principal rate limit and the two curated 503 paths."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import delete

from mcprouter.api.app import create_app
from mcprouter.db import make_engine, make_session_factory
from mcprouter.inference.errors import InferenceError
from mcprouter.models import AgentPrincipal
from tests.support.edge import KEY, _settings
from tests.support.edge_app import AUTH, PATH, edge_client

from .conftest import requires_db

pytestmark = requires_db

BODY = {"state": "s", "questions": {"q0": {"type": "noul", "instructions": "x"}}}
OTHER_KEY = "edge-other-key-0123456789abcdef"
OTHER = {"Authorization": f"Bearer {OTHER_KEY}"}


@contextmanager
def _two_agent_edge(**kw: object) -> Iterator[TestClient]:
    keys = f"edgebot:{KEY},edgebot2:{OTHER_KEY}"
    s = _settings(**{"decision_backend": "deterministic", "embedding_backend": "hash", **kw})
    s = dataclasses.replace(s, agent_keys=keys)
    app = create_app(s, env={"MCPR_AGENT_KEYS": keys})
    try:
        with TestClient(app) as client:
            yield client
    finally:
        eng = make_engine(s)
        try:
            with make_session_factory(eng)() as session:
                session.execute(
                    delete(AgentPrincipal).where(
                        AgentPrincipal.agent_id.in_(["edgebot", "edgebot2"])
                    )
                )
                session.commit()
        finally:
            eng.dispose()


def test_rate_limit_is_per_principal_and_uses_the_setting() -> None:
    with _two_agent_edge(decision_rate_limit_per_min=2) as client:
        codes = [client.post(PATH, json=BODY, headers=AUTH).status_code for _ in range(3)]
        assert codes == [200, 200, 429]
        assert client.post(PATH, json=BODY, headers=OTHER).status_code == 200


def test_no_inference_engine_is_a_curated_503() -> None:
    with edge_client() as (app, client):
        app.state.inference_engine = None
        r = client.post(PATH, json=BODY, headers=AUTH)
    assert r.status_code == 503
    assert r.json() == {"detail": "inference engine is not configured"}


class _Failing:
    name = "failing"

    def noul(self, *_a: object, **_k: object) -> float:
        raise InferenceError("backend exploded: token=leakme")


def test_inference_error_is_a_curated_503() -> None:
    with edge_client() as (app, client):
        app.state.decision_model = _Failing()
        r = client.post(PATH, json=BODY, headers=AUTH)
    assert r.status_code == 503
    assert r.json() == {"detail": "decision backend unavailable; retry later"}
    assert "leakme" not in r.text
