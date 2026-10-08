"""Edge-capacity proof: router A serves its deterministic decider on
/api/v1/decision/systemone; RemoteSystemOneModel (router B's backend) pointed at
it returns exactly what the direct deterministic model returns."""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from sqlalchemy import delete

from mcprouter.api.app import create_app
from mcprouter.db import make_engine, make_session_factory
from mcprouter.inference.deterministic import DeterministicDecisionModel
from mcprouter.inference.remote_systemone import RemoteAuthError, RemoteSystemOneModel
from mcprouter.models import AgentPrincipal
from mcprouter.settings import Settings

PORT_A, PORT_LOOP = 8761, 8762
KEY = "edge-test-key-0123456789abcdef"
STATE = "user wants to read a file from the repository and summarize it"
OPTS = ["read_file", "send_email", "delete_repo"]
LEVELS = ["none", "low", "medium", "high"]


@contextmanager
def _serve(settings: Settings, port: int) -> Iterator[None]:
    app = create_app(settings, env={"MCPR_AGENT_KEYS": f"edgebot:{KEY}"})
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    try:
        yield
    finally:
        server.should_exit = True
        t.join(timeout=10)
        _drop_principal(settings)


def _drop_principal(settings: Settings) -> None:
    """The bootstrapped principal would take later tests out of dev mode."""
    eng = make_engine(settings)
    try:
        with make_session_factory(eng)() as session:
            session.execute(delete(AgentPrincipal).where(AgentPrincipal.agent_id == "edgebot"))
            session.commit()
    finally:
        eng.dispose()


def _settings(**kw: object) -> Settings:
    base = Settings.from_env()
    return dataclasses.replace(base, agent_keys=f"edgebot:{KEY}", **kw)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def edge() -> Iterator[str]:
    with _serve(_settings(decision_backend="deterministic", embedding_backend="hash"), PORT_A):
        yield f"http://127.0.0.1:{PORT_A}/api/v1/decision/systemone"


def test_remote_client_round_trips_deterministic_answers(edge: str) -> None:
    remote = RemoteSystemOneModel(endpoint=edge, api_key=KEY, timeout_s=10.0, max_retries=0)
    direct = DeterministicDecisionModel()
    rc, dc = remote.choice(STATE, "which tool?", OPTS), direct.choice(STATE, "which tool?", OPTS)
    assert rc.option == dc.option
    assert rc.probabilities == pytest.approx(dc.probabilities)
    rs, ds = remote.score(STATE, "how risky?", LEVELS), direct.score(STATE, "how risky?", LEVELS)
    assert rs.level == ds.level and rs.probabilities == pytest.approx(ds.probabilities)
    qs = ["is it relevant?", "is it destructive?", "is it cheap?"]
    rb, db = remote.score_batch(STATE, qs, LEVELS), direct.score_batch(STATE, qs, LEVELS)
    assert [r.level for r in rb] == [d.level for d in db]
    for r, d in zip(rb, db, strict=True):
        assert r.probabilities == pytest.approx(d.probabilities)
    assert remote.noul(STATE, "is the request clear?") == pytest.approx(
        direct.noul(STATE, "is the request clear?")
    )


def test_unauthenticated_is_401(edge: str) -> None:
    body = {"state": "s", "questions": {"q0": {"type": "noul", "instructions": "x"}}}
    assert httpx.post(edge, json=body).status_code == 401
    bad = httpx.post(edge, json=body, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    with pytest.raises(RemoteAuthError):
        RemoteSystemOneModel(endpoint=edge, api_key="wrong-key", max_retries=0).noul("s", "x")


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"state": "x" * 32_769, "questions": {"q": {"type": "noul", "instructions": "x"}}}, 422),
        (
            {
                "state": "s",
                "questions": {f"q{i}": {"type": "noul", "instructions": "x"} for i in range(33)},
            },
            422,
        ),
        (
            {
                "state": "s",
                "questions": {
                    "q": {
                        "type": "choice",
                        "instructions": "x",
                        "criteria": [str(i) for i in range(256)],
                    }
                },
            },
            422,
        ),
        (
            {
                "state": "s",
                "questions": {
                    "q": {
                        "type": "choice",
                        "instructions": "x",
                        "criteria": [str(i) for i in range(255)],
                    }
                },
            },
            200,
        ),
        ({"state": "s", "questions": {"q": {"type": "bogus", "instructions": "x"}}}, 422),
    ],
)
def test_body_caps(edge: str, body: dict[str, object], code: int) -> None:
    r = httpx.post(edge, json=body, headers={"Authorization": f"Bearer {KEY}"}, timeout=10)
    assert r.status_code == code


def test_loop_guard_refuses_self_pointing_remote() -> None:
    url = f"http://localhost:{PORT_LOOP}/api/v1/decision/systemone"
    s = _settings(decision_backend="remote", decision_endpoint=url, decision_api_key=KEY)
    with _serve(s, PORT_LOOP):
        body = {"state": "s", "questions": {"q0": {"type": "noul", "instructions": "x"}}}
        r = httpx.post(
            f"http://127.0.0.1:{PORT_LOOP}/api/v1/decision/systemone",
            json=body,
            headers={"Authorization": f"Bearer {KEY}"},
            timeout=10,
        )
        assert r.status_code == 503
        assert "points at this router" in r.json()["detail"]
