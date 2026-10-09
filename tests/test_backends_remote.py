"""Remote System One decision backend (wave 3) against httpx.MockTransport,
using the documented typesafe/jev request/response shapes."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from mcprouter.inference.errors import DecisionRuntimeError
from mcprouter.inference.remote_systemone import (
    MAX_RESPONSE_BYTES,
    RemoteAuthError,
    RemoteDecisionError,
    RemoteKeyFormatError,
    RemoteRateLimitedError,
    RemoteResponseError,
    RemoteSystemOneModel,
    RemoteTimeoutError,
    resolve_endpoint,
)
from mcprouter.inference.urlcheck import InvalidEndpointError, validate_outbound_url
from mcprouter.inference.validation import ValidatedDecisionModel
from mcprouter.interfaces import BatchScoringDecisionModel, DecisionModel
from tests.support.remote import (
    _trickle_server,
)

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
    with pytest.raises(RemoteResponseError):  # refused in-backend now
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
    assert_no_key_in_chain(ei.value)


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


# ------------------------------------------------- wave-3 review fixes
def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    todo: list[BaseException | None] = [exc]
    while todo:
        e = todo.pop()
        if e is None or any(e is x for x in seen):
            continue
        seen.append(e)
        todo += [e.__cause__, e.__context__]
    return seen


def assert_no_key_in_chain(exc: BaseException) -> None:
    """R1: no exception reachable via __cause__/__context__ may expose the key
    (repr/str/args/attributes, including an httpx ``.request.headers``)."""
    for e in _chain(exc):
        assert not isinstance(e, httpx.HTTPError), f"httpx error chained: {type(e)}"
        blob = repr(e) + str(e) + repr(e.args) + repr(vars(e))
        req = getattr(e, "_request", None) or getattr(e, "request", None)
        if isinstance(req, httpx.Request):
            blob += repr(dict(req.headers))
        assert KEY not in blob


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError]
)
def test_transport_errors_do_not_chain_the_key(exc: type[httpx.HTTPError]) -> None:
    def h(r: httpx.Request) -> httpx.Response:
        raise exc("boom", request=r)

    m, _ = make(h)
    with pytest.raises(RemoteDecisionError) as ei:
        m.noul("s", "q")
    assert_no_key_in_chain(ei.value)


def test_status_errors_do_not_chain_the_key() -> None:
    for status in (401, 418, 503):
        m, _ = make(lambda r, s=status: httpx.Response(s), max_retries=0)
        with pytest.raises(RemoteDecisionError) as ei:
            m.noul("s", "q")
        assert_no_key_in_chain(ei.value)


class _FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_retry_sleep_past_deadline_is_refused() -> None:
    clock = _FakeClock()
    m = RemoteSystemOneModel(
        endpoint=EP,
        api_key=KEY,
        timeout_s=1.0,
        max_retries=5,
        clock=clock,
        transport=httpx.MockTransport(lambda r: httpx.Response(503, headers={"retry-after": "2"})),
        sleep=lambda d: setattr(clock, "t", clock.t + d),
    )
    with pytest.raises(RemoteTimeoutError):
        m.noul("s", "q")
    assert clock.t <= 101.0


def test_slow_drip_body_hits_total_deadline() -> None:
    clock = _FakeClock()

    class Drip(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            for _ in range(100):
                clock.t += 0.1  # each chunk arrives 0.1s later; per-chunk timeout never fires
                yield b" "

    m = RemoteSystemOneModel(
        endpoint=EP,
        api_key=KEY,
        timeout_s=1.0,
        clock=clock,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Drip())),
    )
    with pytest.raises(RemoteTimeoutError):
        m.noul("s", "q")
    assert clock.t <= 101.0 + 0.1 + 1e-9


def test_slow_drip_real_time_budget() -> None:
    import time as _t

    class Drip(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            for _ in range(40):
                _t.sleep(0.05)
                yield b" "

    m = RemoteSystemOneModel(
        endpoint=EP,
        api_key=KEY,
        timeout_s=0.3,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Drip())),
    )
    t0 = _t.monotonic()
    with pytest.raises(RemoteTimeoutError):
        m.noul("s", "q")
    # The hard stop is what matters: the unfixed path took ~9 s against a
    # 1 s budget. 0.5 s of slack absorbs scheduler latency on a loaded host
    # (seen: +0.18 s under 8 xdist workers) without hiding a real regression.
    assert _t.monotonic() - t0 <= 0.3 + 0.5


def test_unknown_option_mass_refused() -> None:
    m, _ = make(
        lambda r: answers(
            q0={
                "type": "choice",
                "choice": "a",
                "probabilities": {"a": 0.01, "b": 0.01, "EVIL": 0.98},
            }
        )
    )
    with pytest.raises(RemoteResponseError, match="unknown options"):
        m.choice("s", "q", ["a", "b"])


def test_unknown_level_mass_refused() -> None:
    m, _ = make(lambda r: answers(q0={"type": "score", "probabilities": {"0": 0.1, "9": 0.9}}))
    with pytest.raises(RemoteResponseError, match="unknown options"):
        m.score("s", "q", ["lo", "hi"])


def test_jitter_never_exceeds_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcprouter.inference.remote_systemone as rs

    monkeypatch.setattr(rs.random, "uniform", lambda a, b: b)  # worst-case jitter
    for attempt in range(4):
        d = RemoteSystemOneModel._retry_delay(attempt, "5")
        assert d is not None and d <= 5.0


@pytest.mark.parametrize(
    "url",
    [
        "https://exa\x00mple.com/v1",
        "https://xn--a.example/v1",  # IDNA-invalid A-label
        "https://[::1/v1",
        "https://a..b-⒈.com/v1",
    ],
)
def test_malformed_hosts_are_curated(url: str) -> None:
    with pytest.raises(InvalidEndpointError) as ei:
        validate_outbound_url(url)
    msg = str(ei.value)
    assert "example" not in msg and "::1" not in msg and "⒈" not in msg
    assert (
        ei.value.__context__ is None
        or not isinstance(ei.value.__context__, Exception)
        or (ei.value.__suppress_context__)
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest",
        "http://169.254.0.1/",
        "https://[fe80::1]/x",
        "https://100.100.100.200/x",
        "https://[fd00:ec2::254]/x",
    ],
)
def test_link_local_and_metadata_literals_refused(url: str) -> None:
    with pytest.raises(InvalidEndpointError, match="link-local or metadata"):
        validate_outbound_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://192.168.1.10/v1",
        "https://10.0.0.2/v1",
        "https://[fd12::1]/v1",
        "https://edge.lan/v1",
    ],
)
def test_private_lan_remote_allowed(url: str) -> None:
    assert validate_outbound_url(url) == url


def test_non_ascii_key_refused_without_echo() -> None:
    with pytest.raises(RemoteKeyFormatError) as ei:
        RemoteSystemOneModel(
            endpoint=EP,
            api_key="sk-SECR\u00c9T",
            transport=httpx.MockTransport(lambda r: answers()),
        )
    assert "SECR" not in str(ei.value)


@pytest.mark.parametrize("hdr", ["retry-after", "content-length"])
def test_superscript_digit_headers_do_not_crash(hdr: str) -> None:
    status = 503 if hdr == "retry-after" else 200
    m, _ = make(
        lambda r: httpx.Response(status, headers={hdr: "\u00b2".encode("latin-1")}, content=b"{}"),
        max_retries=1,
    )
    with pytest.raises(RemoteDecisionError):
        m.noul("s", "q")


# ------------------------------------------------------------ wave-3 FIX-2


def test_trickled_headers_hit_the_hard_stop() -> None:
    import time as _time

    url, shutdown = _trickle_server(0.3)
    m = RemoteSystemOneModel(endpoint=url, api_key=KEY, timeout_s=0.5, max_retries=0)
    t0 = _time.monotonic()
    try:
        with pytest.raises(RemoteTimeoutError):
            m.noul("s", "q")
    finally:
        shutdown()
    elapsed = _time.monotonic() - t0
    assert elapsed < 0.5 + 0.3 + 0.2, elapsed


@pytest.mark.parametrize("bad", ["sk-été", "sk key", "sk\tkey", "sk\x7fkey", "k "])
def test_non_printable_key_refused_without_leaking(bad: str) -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return answers(q0={"type": "noul", "noul": 0.5})

    with pytest.raises(RemoteKeyFormatError) as ei:
        RemoteSystemOneModel(endpoint=EP, api_key=bad, transport=httpx.MockTransport(handler))
    assert not calls
    exc: BaseException | None = ei.value
    while exc is not None:
        assert bad not in repr(exc) and bad not in str(exc)
        assert all(bad not in repr(a) for a in exc.args)
        assert getattr(exc, "object", None) is None
        exc = exc.__cause__ or exc.__context__


@pytest.mark.parametrize("hdr", ["\xb2", "1\xb2", "٣", "9" * 5000])
def test_hostile_retry_after_is_not_a_crash(hdr: str) -> None:
    n = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(503, headers={"retry-after": hdr.encode("latin-1", "replace")})
        return answers(q0={"type": "noul", "noul": 0.5})

    m, _ = make(handler, timeout_s=30.0)
    try:
        assert m.noul("s", "q") == 0.5
    except RemoteRateLimitedError:
        assert hdr == "9" * 5000  # absurd Retry-After: fail fast, never ValueError


@pytest.mark.parametrize("hdr", [b"\xb2", b"1\xb2", b"9" * 5000])
def test_hostile_content_length_is_typed(hdr: bytes) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.dumps({"answers": {"q0": {"type": "noul", "noul": 0.5}}}).encode()
        return httpx.Response(
            200, headers=[(b"content-length", hdr)], stream=httpx.ByteStream(body)
        )

    m, _ = make(handler)
    try:
        m.noul("s", "q")
    except (RemoteResponseError, RemoteDecisionError):
        pass


@pytest.mark.parametrize(
    "host",
    [
        "2852039166",
        "0xa9fea9fe",
        "0251.0376.0251.0376",
        "169.254.43518",
        "169.16689662",
        "[::a9fe:a9fe]",
        "[::ffff:0:a9fe:a9fe]",
        "[64:ff9b::a9fe:a9fe]",
        "[::ffff:169.254.169.254]",
        "[::ffff:100.100.100.200]",
        "1684301000",  # 100.100.100.200 as decimal
        "[64:ff9b:1::a00:5]",  # NAT64 local-use (RFC 8215)
        "[64:ff9b:1::1]",
    ],
)
def test_alternative_metadata_encodings_refused(host: str) -> None:
    with pytest.raises(InvalidEndpointError):
        validate_outbound_url(f"https://{host}/v1/decisions")


@pytest.mark.parametrize("host", ["10.0.0.5", "[fd12::1]", "[::1]", "edge.lan", "[64:ff9b::a00:5]"])
def test_lan_and_names_still_allowed(host: str) -> None:
    assert validate_outbound_url(f"https://{host}/v1/decisions")


def test_key_absent_from_traceback_locals() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=req)

    m, _ = make(handler, max_retries=0)
    with pytest.raises(RemoteDecisionError) as ei:
        m.noul("s", "q")
    tb = ei.value.__traceback__
    seen = 0
    while tb is not None:
        for v in tb.tb_frame.f_locals.values():
            assert KEY not in repr(v)
            if isinstance(v, dict):
                assert all(KEY not in repr(x) for x in v.values())
        seen += 1
        tb = tb.tb_next
    assert seen >= 2


def test_choice_outside_options_refused() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return answers(q0={"type": "choice", "choice": "zzz", "probabilities": {"a": 1.0}})

    m, _ = make(handler)
    with pytest.raises(RemoteResponseError):
        m.choice("s", "q", ["a", "b"])


def test_hard_stop_before_send_is_a_timeout() -> None:
    url, shutdown = _trickle_server(0.3)
    m = RemoteSystemOneModel(endpoint=url, api_key=KEY, timeout_s=1.0, max_retries=0)
    try:
        for _ in range(200):
            with pytest.raises(RemoteTimeoutError):
                m._attempt(url, b"{}", 1e-6, 0.0)
    finally:
        shutdown()


def _assert_key_unreachable(v: Any, depth: int = 0) -> None:
    """Walk a frame local deep enough to reach a held request's headers."""
    if depth > 3:
        return
    assert KEY not in repr(v)
    if isinstance(v, httpx.Response):
        assert KEY not in repr(dict(v.request.headers))
    elif isinstance(v, httpx.Request):
        assert KEY not in repr(dict(v.headers))
    elif isinstance(v, dict):
        for x in v.values():
            _assert_key_unreachable(x, depth + 1)
    elif isinstance(v, (list, tuple)):
        for x in v:
            _assert_key_unreachable(x, depth + 1)


def test_key_absent_from_locals_on_body_error_path() -> None:
    """An error raised while the response is open (oversized body) must not
    leave ``resp`` (whose request headers carry the key) in any frame."""
    big = b'{"pad": "' + b"x" * MAX_RESPONSE_BYTES + b'"}'
    m, _ = make(lambda r: httpx.Response(200, content=big))
    with pytest.raises(RemoteResponseError) as ei:
        m.noul("s", "q")
    tb = ei.value.__traceback__
    names: set[str] = set()
    while tb is not None:
        for name, v in tb.tb_frame.f_locals.items():
            names.add(name)
            _assert_key_unreachable(v)
        tb = tb.tb_next
    assert "holder" in names  # the _attempt frame was actually inspected
