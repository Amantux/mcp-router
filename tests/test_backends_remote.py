"""Remote System One decision backend (wave 3) against httpx.MockTransport,
using the documented typesafe/jev request/response shapes."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from mcprouter.inference.errors import DecisionProtocolError, DecisionRuntimeError
from mcprouter.inference.remote_systemone import (
    MAX_RESPONSE_BYTES,
    RemoteAuthError,
    RemoteDecisionError,
    RemoteRateLimitedError,
    RemoteResponseError,
    RemoteSystemOneModel,
    RemoteTimeoutError,
    resolve_endpoint,
)
from mcprouter.inference.urlcheck import InvalidEndpointError, validate_outbound_url
from mcprouter.inference.validation import ValidatedDecisionModel
from mcprouter.interfaces import BatchScoringDecisionModel, DecisionModel

KEY = "sk-test-Zq8vR2mN4pL6tY1wX3cB5dF7gH9jK0aS"
EP = "https://api.aimlapi.com/v1/decisions"
Handler = Callable[[httpx.Request], httpx.Response]


def make(handler: Handler, **kw: Any) -> tuple[RemoteSystemOneModel, list[float]]:
    sleeps: list[float] = []
    kw.setdefault("endpoint", EP)
    m = RemoteSystemOneModel(
        api_key=KEY, transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw
    )
    return m, sleeps


def answers(**a: Any) -> httpx.Response:
    return httpx.Response(
        200, json={"model": "typesafe/jev-1.13-20260917", "answers": a, "usage": {}}
    )


# ------------------------------------------------------------- round-trips
def test_protocol_conformance() -> None:
    m, _ = make(lambda r: answers())
    assert isinstance(m, DecisionModel) and isinstance(m, BatchScoringDecisionModel)
    assert m.name == "remote:typesafe/jev"
    assert KEY not in repr(m)


def test_choice_round_trip() -> None:
    seen: list[dict[str, Any]] = []

    def h(r: httpx.Request) -> httpx.Response:
        assert r.headers["authorization"] == f"Bearer {KEY}"
        assert r.url.path == "/v1/decisions"
        seen.append(json.loads(r.content))
        probs = {"billing": 0.6, "tech": 0.2}  # "sales" missing -> 0, renormalized
        return answers(
            q0={"type": "choice", "choice": "billing", "confidence": 0.9, "probabilities": probs}
        )

    m, _ = make(h)
    res = ValidatedDecisionModel(m).choice("state", "Which team?", ["billing", "tech", "sales"])
    assert res.option == "billing"
    assert res.probabilities == pytest.approx({"billing": 0.75, "tech": 0.25, "sales": 0.0})
    q = seen[0]["questions"]["q0"]
    assert seen[0]["model"] == "typesafe/jev"
    assert q == {
        "type": "choice",
        "instructions": "Which team?",
        "criteria": {"billing": "billing", "tech": "tech", "sales": "sales"},
    }


def test_novel_option_refused_by_validator() -> None:
    def h(r: httpx.Request) -> httpx.Response:
        return answers(
            q0={"type": "choice", "choice": "marketing", "probabilities": {"a": 0.5, "b": 0.5}}
        )

    m, _ = make(h)
    with pytest.raises(DecisionProtocolError):
        ValidatedDecisionModel(m).choice("s", "q", ["a", "b"])


def test_score_uses_argmax_not_fractional_score() -> None:
    def h(r: httpx.Request) -> httpx.Response:
        body = json.loads(r.content)
        assert body["questions"]["q0"]["criteria"] == ["Calm", "Frustrated", "Very angry"]
        return answers(
            q0={
                "type": "score",
                "score": 1.3,
                "confidence": 0.55,
                "legend": {"0": "Calm", "1": "Frustrated", "2": "Very angry"},
                "probabilities": {"0": 0, "1": 0.3, "2": 0.7},
            }
        )

    m, _ = make(h)
    res = ValidatedDecisionModel(m).score("s", "How angry?", ["Calm", "Frustrated", "Very angry"])
    assert res.level == 2  # round(1.3) would say 1
    assert res.probabilities == pytest.approx([0.0, 0.3, 0.7])


def test_score_batch_is_one_request() -> None:
    calls: list[dict[str, Any]] = []

    def h(r: httpx.Request) -> httpx.Response:
        body = json.loads(r.content)
        calls.append(body)
        out = {
            k: {
                "type": "score",
                "probabilities": {"0": 0.1, "1": 0.9} if k == "q1" else {"0": 0.8, "1": 0.2},
            }
            for k in body["questions"]
        }
        return answers(**out)

    m, _ = make(h)
    res = ValidatedDecisionModel(m).score_batch("s", ["a?", "b?", "c?"], ["no", "yes"])
    assert len(calls) == 1 and len(calls[0]["questions"]) == 3
    assert [r.level for r in res] == [0, 1, 0]
    assert m.score_batch("s", [], ["no", "yes"]) == []


def test_noul_round_trip_and_bounds() -> None:
    value: list[Any] = [0.96]

    def h(r: httpx.Request) -> httpx.Response:
        assert json.loads(r.content)["questions"]["q0"] == {
            "type": "noul",
            "instructions": "urgent?",
        }
        return answers(q0={"type": "noul", "noul": value[0]})

    m, _ = make(h)
    assert ValidatedDecisionModel(m).noul("s", "urgent?") == pytest.approx(0.96)
    for bad in (1.5, "0.5", None, True):
        value[0] = bad
        with pytest.raises(RemoteResponseError):
            m.noul("s", "urgent?")


def test_missing_or_mistyped_answer_is_typed_error() -> None:
    m, _ = make(lambda r: answers(q0={"type": "score", "probabilities": {"0": 1}}))
    with pytest.raises(RemoteResponseError):
        m.noul("s", "q")


# ------------------------------------------------------------- retries
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_retries_then_succeeds(status: int) -> None:
    n = [0]

    def h(r: httpx.Request) -> httpx.Response:
        n[0] += 1
        if n[0] <= 2:
            return httpx.Response(status, headers={"retry-after": "1"}, text="upstream secret body")
        return answers(q0={"type": "noul", "noul": 0.5})

    m, sleeps = make(h, max_retries=2)
    assert m.noul("s", "q") == 0.5
    assert n[0] == 3 and len(sleeps) == 2 and all(s >= 1.0 for s in sleeps)


def test_retries_are_bounded() -> None:
    n = [0]

    def h(r: httpx.Request) -> httpx.Response:
        n[0] += 1
        return httpx.Response(503, text="upstream secret body")

    m, sleeps = make(h, max_retries=2)
    with pytest.raises(RemoteRateLimitedError) as ei:
        m.noul("s", "q")
    assert n[0] == 3 and len(sleeps) == 2
    assert "upstream secret body" not in str(ei.value)


def test_long_retry_after_fails_fast() -> None:
    n = [0]

    def h(r: httpx.Request) -> httpx.Response:
        n[0] += 1
        return httpx.Response(429, headers={"retry-after": "3600"})

    m, sleeps = make(h, max_retries=5)
    with pytest.raises(RemoteRateLimitedError):
        m.noul("s", "q")
    assert n[0] == 1 and sleeps == []


@pytest.mark.parametrize("status", [400, 404, 409, 422])
def test_other_4xx_never_retried(status: int) -> None:
    n = [0]

    def h(r: httpx.Request) -> httpx.Response:
        n[0] += 1
        return httpx.Response(status, text="upstream secret body")

    m, sleeps = make(h, max_retries=3)
    with pytest.raises(RemoteDecisionError) as ei:
        m.noul("s", "q")
    assert n[0] == 1 and sleeps == []
    assert "upstream secret body" not in str(ei.value)


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_typed_not_retried(status: int) -> None:
    n = [0]

    def h(r: httpx.Request) -> httpx.Response:
        n[0] += 1
        return httpx.Response(status)

    m, _ = make(h)
    with pytest.raises(RemoteAuthError):
        m.noul("s", "q")
    assert n[0] == 1


# ------------------------------------------------------------- failures
def test_timeout_is_typed_and_falls_back() -> None:
    def h(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=r)

    m, sleeps = make(h)
    with pytest.raises(RemoteTimeoutError) as ei:
        m.noul("s", "q")
    assert isinstance(ei.value, DecisionRuntimeError) and sleeps == []
    assert ei.value.__cause__ is None and ei.value.__suppress_context__


def test_malformed_json_is_typed() -> None:
    m, _ = make(lambda r: httpx.Response(200, content=b"{not json"))
    with pytest.raises(RemoteResponseError, match="not valid JSON"):
        m.noul("s", "q")


def test_oversized_body_refused() -> None:
    big = (
        b'{"answers": {"q0": {"type": "noul", "noul": 0.5}}, "pad": "'
        + b"x" * MAX_RESPONSE_BYTES
        + b'"}'
    )
    m, _ = make(lambda r: httpx.Response(200, content=big))
    with pytest.raises(RemoteResponseError, match="too large"):
        m.noul("s", "q")


def test_deep_nesting_refused() -> None:
    deep = (
        b'{"answers": {"q0": {"type": "noul", "noul": 0.5}}, "x": ' + b"[" * 25 + b"]" * 25 + b"}"
    )
    m, _ = make(lambda r: httpx.Response(200, content=deep))
    with pytest.raises(RemoteResponseError, match="nested"):
        m.noul("s", "q")
    shallow = b'{"answers": {"q0": {"type": "noul", "noul": 0.5}}, "x": "[[[[[[[[[[[[[[[[[[[[[["}'
    m2, _ = make(lambda r: httpx.Response(200, content=shallow))
    assert m2.noul("s", "q") == 0.5  # brackets inside strings do not count


def test_missing_key_is_auth_error() -> None:
    m = RemoteSystemOneModel(
        endpoint=EP, api_key="", transport=httpx.MockTransport(lambda r: answers())
    )
    with pytest.raises(RemoteAuthError):
        m.noul("s", "q")


# ------------------------------------------------------------- secrets
def test_state_is_redacted_before_sending() -> None:
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    sent: list[bytes] = []

    def h(r: httpx.Request) -> httpx.Response:
        sent.append(r.content)
        return answers(q0={"type": "noul", "noul": 0.1})

    m, _ = make(h)
    m.noul(f"deploy with token {secret} please", "q")
    assert secret not in sent[0].decode()
    assert b"deploy with token" in sent[0]


def test_key_never_in_logs_or_errors(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    errors: list[str] = []
    for status in (401, 429, 500, 400):
        m, _ = make(
            lambda r, s=status: httpx.Response(s, headers={"x-request-id": "req-1"}), max_retries=1
        )
        with pytest.raises(RemoteDecisionError) as ei:
            m.noul("s", "q")
        errors.append(repr(ei.value) + str(ei.value))
    text = caplog.text + "".join(errors)
    assert "req-1" in caplog.text
    for i in range(len(KEY) - 15):
        assert KEY[i : i + 16] not in text


# ------------------------------------------------------------- url validation
@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1/decisions",
        "ftp://example.com/x",
        "https://user:pw@example.com/v1",
        "https://example.com/v1#frag",
        "https://exa mple.com/v1",
        "https:///v1",
        "",
    ],
)
def test_validator_rejects(url: str) -> None:
    with pytest.raises(InvalidEndpointError):
        validate_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.aimlapi.com/v1/decisions",
        "http://localhost:8765/v1",
        "http://127.0.0.1/x",
        "http://[::1]:9/x",
    ],
)
def test_validator_accepts(url: str) -> None:
    assert validate_outbound_url(url) == url


def test_validator_http_localhost_can_be_disabled() -> None:
    with pytest.raises(InvalidEndpointError):
        validate_outbound_url("http://localhost/x", allow_http_localhost=False)


def test_endpoint_validated_at_point_of_use() -> None:
    n = [0]

    def h(r: httpx.Request) -> httpx.Response:
        n[0] += 1
        return answers(q0={"type": "noul", "noul": 0.5})

    m, _ = make(h, endpoint="http://example.com/v1/decisions")  # construction does not validate
    with pytest.raises(InvalidEndpointError):
        m.noul("s", "q")
    assert n[0] == 0


def test_resolve_endpoint_appends_default_path() -> None:
    assert resolve_endpoint("https://edge.local") == "https://edge.local/v1/decisions"
    assert resolve_endpoint("https://edge.local/") == "https://edge.local/v1/decisions"
    assert resolve_endpoint("https://edge.local/custom") == "https://edge.local/custom"


def test_oversized_streamed_body_without_content_length_refused() -> None:
    def chunks() -> Any:
        yield b'{"answers": {"q0": {"type": "noul", "noul": 0.5}}, "pad": "'
        for _ in range(20):
            yield b"x" * (MAX_RESPONSE_BYTES // 16)
        yield b'"}'

    def h(r: httpx.Request) -> httpx.Response:
        resp = httpx.Response(200, content=chunks())
        assert "content-length" not in resp.headers
        return resp

    m, _ = make(h)
    with pytest.raises(RemoteResponseError, match="too large"):
        m.noul("s", "q")
