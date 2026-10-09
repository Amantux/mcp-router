"""Hop-count loop guard: MCPR->MCPR decision calls carry X-MCPR-Decision-Hop."""

from __future__ import annotations

import httpx
import pytest

from mcprouter.api.routes_decision import parse_hop
from mcprouter.inference.remote_systemone import DECISION_HOP, RemoteSystemOneModel
from tests.support.edge_app import AUTH, PATH, edge_client, served_edge

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
    assert r.json()["detail"] == REFUSED


# Malformed hops as raw header BYTES over a real socket: uvicorn decodes header
# values latin-1, so b"\xb2" arrives as "²", which isdigit() accepts and int()
# rejects. (Starlette's TestClient re-encodes header values, so it cannot send
# these; the edge is served for real via tests/support/edge_app.served_edge.)
MALFORMED_HOPS: list[bytes] = [b"\xb2", b"\xb9", b"9" * 5000, b"-1", b"1.0", b"0x1", b"1 1"]
REFUSED = "decision edge refused: request already forwarded by a router"


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ({"decision_backend": "remote", "decision_endpoint": "http://other.invalid:9/x"}, 503),
        ({}, 200),
    ],
    ids=["remote-refuses", "local-serves"],
)
def test_malformed_hop_over_real_http_is_never_500(backend: dict[str, str], expected: int) -> None:
    extra = {"decision_api_key": "k" * 32} if backend else {}
    with (
        served_edge(**backend, **extra) as (app, base_url),
        httpx.Client(base_url=base_url) as http,
    ):
        if backend:
            app.state.decision_model = _Spy()
        got = {
            hop[:8]: http.post(
                PATH,
                json=BODY,
                headers=[(k.encode(), v.encode()) for k, v in AUTH.items()]
                + [(b"X-MCPR-Decision-Hop", hop)],
            ).status_code
            for hop in MALFORMED_HOPS
        }
    assert got == {hop[:8]: expected for hop in MALFORMED_HOPS}


@pytest.mark.parametrize(
    ("raw", "hop"),
    [
        (None, 0),
        ("0", 0),
        ("1", 1),
        ("007", 7),
        ("999", 999),
        ("1000", 1),  # > 3 digits: malformed
        ("\u00b2", 1),  # superscript two: isdigit() but not decimal
        ("\u0663", 1),  # Arabic-Indic three: isdecimal() but not ASCII
        ("9" * 5000, 1),  # beyond int()'s 4300-digit limit
        (" 1", 1),
        ("-1", 1),
        ("1.0", 1),
        ("", 1),
    ],
    ids=lambda v: repr(v)[:12],
)
def test_parse_hop_table(raw: str | None, hop: int) -> None:
    assert parse_hop(raw) == hop


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
