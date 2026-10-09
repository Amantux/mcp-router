"""Edge-capacity proof: router A serves its deterministic decider on
/api/v1/decision/systemone; RemoteSystemOneModel (router B's backend) pointed at
it returns exactly what the direct deterministic model returns."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator

import httpx
import pytest

from mcprouter.inference.deterministic import DeterministicDecisionModel
from mcprouter.inference.remote_systemone import RemoteAuthError, RemoteSystemOneModel
from tests.support.edge import KEY
from tests.support.edge_app import PATH, served_edge

STATE = "user wants to read a file from the repository and summarize it"
OPTS = ["read_file", "send_email", "delete_repo"]
LEVELS = ["none", "low", "medium", "high"]


@pytest.fixture(scope="module")
def edge() -> Iterator[str]:
    with served_edge() as (_, base_url):
        yield f"{base_url}{PATH}"


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
    remote = {"decision_backend": "remote", "decision_api_key": KEY}
    with served_edge(**remote, decision_endpoint="http://localhost:9/x") as (app, base_url):
        # Point our own remote backend at the port we were actually given
        # (settings are read per request; the port is OS-assigned).
        port = base_url.rsplit(":", 1)[1]
        app.state.settings = dataclasses.replace(
            app.state.settings, decision_endpoint=f"http://localhost:{port}{PATH}"
        )
        body = {"state": "s", "questions": {"q0": {"type": "noul", "instructions": "x"}}}
        r = httpx.post(
            f"{base_url}{PATH}", json=body, headers={"Authorization": f"Bearer {KEY}"}, timeout=10
        )
        assert r.status_code == 503
        assert "points at this router" in r.json()["detail"]
