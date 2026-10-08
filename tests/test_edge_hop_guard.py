"""Hop-count loop guard: MCPR->MCPR decision calls carry X-MCPR-Decision-Hop."""

from __future__ import annotations

import httpx
import pytest

from mcprouter.inference.remote_systemone import DECISION_HOP, RemoteSystemOneModel
from tests.edge_app_helpers import AUTH, PATH, edge_client

BODY = {"state": "s", "questions": {"q0": {"type": "noul", "instructions": "x"}}}


class _Spy:
    name = "spy"

    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, attr: str) -> object:
        raise AssertionError(f"model touched: {attr}")


@pytest.mark.parametrize("hop", ["1", "7", "junk"])
def test_edge_refuses_forwarded_hop_before_any_model_call(hop: str) -> None:
    remote = {"decision_backend": "remote", "decision_endpoint": "http://other.invalid:9/x"}
    with edge_client(**remote, decision_api_key="k" * 32) as (app, client):
        app.state.decision_model = _Spy()
        r = client.post(PATH, json=BODY, headers={**AUTH, "X-MCPR-Decision-Hop": hop})
    assert r.status_code == 503
    assert r.json()["detail"] == "decision edge refused: request already forwarded by a router"


@pytest.mark.parametrize("hop", ["0", "1"])
def test_one_hop_to_a_local_decider_is_served(hop: str) -> None:
    with edge_client() as (_, client):
        r = client.post(PATH, json=BODY, headers={**AUTH, "X-MCPR-Decision-Hop": hop})
    assert r.status_code == 200


@pytest.mark.parametrize("current", [0, 3])
def test_remote_client_sends_next_hop(current: int) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-mcpr-decision-hop", ""))
        return httpx.Response(500)

    remote = RemoteSystemOneModel(
        endpoint="https://edge.test/api/v1/decision/systemone",
        api_key="k" * 32,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    token = DECISION_HOP.set(current)
    try:
        with pytest.raises(Exception):  # noqa: B017 — only the header matters here
            remote.noul("s", "x")
    finally:
        DECISION_HOP.reset(token)
    assert seen == [str(current + 1)]
