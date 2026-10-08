"""Azure OpenAI backends: every path via httpx.MockTransport (no network)."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
import pytest

from mcprouter.inference.aoai import (
    AoaiConfigError,
    AoaiDecisionModel,
    AoaiEmbeddingBackend,
    _validate_aoai_endpoint,
    build_aoai_decision_model,
)
from mcprouter.inference.errors import (
    DecisionProtocolError,
    DecisionRuntimeError,
    EmbeddingDimensionError,
    EmbeddingRuntimeError,
)
from mcprouter.settings import AoaiSettings

KEY = "sk-test-SECRET-key-value-0123456789"
CFG = AoaiSettings(
    endpoint="https://res.openai.azure.com",
    api_key=KEY,
    chat_deployment="gpt-mini",
    embedding_deployment="te3s",
    max_retries=2,
)


class Recorder:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def body(self, i: int = 0) -> Any:
        return json.loads(self.requests[i].content)


def chat(obj: Any) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(obj)}}]})


def dm(rec: Recorder, cfg: AoaiSettings = CFG) -> Any:
    return build_aoai_decision_model(
        cfg, 2.0, transport=httpx.MockTransport(rec), sleep=lambda s: None
    )


def emb(rec: Recorder) -> AoaiEmbeddingBackend:
    return AoaiEmbeddingBackend(CFG, transport=httpx.MockTransport(rec), sleep=lambda s: None)


# ------------------------------------------------------------- endpoint
@pytest.mark.parametrize(
    "url",
    [
        "https://res.openai.azure.com",
        "https://res.openai.azure.com/",
        "https://my-res.services.ai.azure.com",
        "https://RES.OpenAI.Azure.com:443",
    ],
)
def test_endpoint_accepts(url: str) -> None:
    assert _validate_aoai_endpoint(url).endswith(".azure.com/openai/v1")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://res.openai.azure.com",
        "https://evil.openai.azure.com.attacker.com",
        "https://attacker.com/.openai.azure.com",
        "https://res.openai.azure.com@attacker.com",
        "https://u:p@res.openai.azure.com",
        "https://res.openai.azure.com./",
        "https://openai.azure.com",
        "https://a.b.openai.azure.com",
        "https://res.openai.azure.com/openai/v1",
        "https://res.openai.azure.com?x=1",
        "https://res.openai.azure.com:8443",
        "https://res.openai.azure.com evil",
        "https://res.openaiXazure.com",
    ],
)
def test_endpoint_rejects(url: str) -> None:
    with pytest.raises(AoaiConfigError):
        _validate_aoai_endpoint(url)


def test_missing_config_is_curated() -> None:
    with pytest.raises(AoaiConfigError):
        AoaiDecisionModel(AoaiSettings(endpoint=CFG.endpoint, chat_deployment="d"), 1.0)
    with pytest.raises(AoaiConfigError):
        AoaiEmbeddingBackend(AoaiSettings(endpoint=CFG.endpoint, api_key="k"))


# ------------------------------------------------------------- decision
def test_choice_wire_shape_and_result() -> None:
    rec = Recorder(chat({"option": "b", "probabilities": [1, 3]}))
    m = dm(rec)
    assert m.name == "aoai:gpt-mini"
    r = m.choice("state", "which?", ["a", "b"])
    assert r.option == "b" and r.probabilities == {"a": 0.25, "b": 0.75}
    req = rec.requests[0]
    assert str(req.url) == "https://res.openai.azure.com/openai/v1/chat/completions"
    assert req.headers["api-key"] == KEY and "api-version" not in str(req.url)
    b = rec.body()
    assert b["model"] == "gpt-mini" and b["temperature"] == 0 and "max_completion_tokens" in b
    rf = b["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["properties"]["option"]["enum"] == ["a", "b"]


def test_choice_novel_option_rejected_by_validator() -> None:
    """Mutation check: if the enum were ignored by the service, the validator still holds."""
    rec = Recorder(chat({"option": "delete_everything", "probabilities": [0.5, 0.5]}))
    with pytest.raises(DecisionProtocolError):
        dm(rec).choice("s", "q", ["a", "b"])


def test_missing_probabilities_uniform_with_argmax_boost() -> None:
    r = dm(Recorder(chat({"option": "c", "probabilities": []}))).choice(
        "s", "q", ["a", "b", "c", "d"]
    )
    assert r.probabilities == {"a": 0.125, "b": 0.125, "c": 0.625, "d": 0.125}


def test_score_and_out_of_range_level() -> None:
    rec = Recorder(
        chat({"level": 2, "probabilities": [0, 1, 3]}), chat({"level": 7, "probabilities": []})
    )
    m = dm(rec)
    r = m.score("s", "q", ["lo", "mid", "hi"])
    assert r.level == 2 and r.probabilities == [0.0, 0.25, 0.75]
    assert rec.body()["response_format"]["json_schema"]["schema"]["properties"]["level"][
        "enum"
    ] == [0, 1, 2]
    with pytest.raises(DecisionProtocolError):
        m.score("s", "q", ["lo", "mid", "hi"])


def test_score_batch_is_sequential_one_request_each() -> None:
    rec = Recorder(*[chat({"level": i, "probabilities": [1, 1]}) for i in (0, 1, 1)])
    out = dm(rec).score_batch("s", ["q1", "q2", "q3"], ["no", "yes"])
    assert [r.level for r in out] == [0, 1, 1] and len(rec.requests) == 3


def test_noul_and_range_enforced() -> None:
    m = dm(Recorder(chat({"p_yes": 0.8}), chat({"p_yes": 1.7})))
    assert m.noul("s", "q") == pytest.approx(0.8)
    with pytest.raises(DecisionProtocolError):
        m.noul("s", "q")


def test_garbage_content_is_runtime_error() -> None:
    bad = httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})
    with pytest.raises(DecisionRuntimeError):
        dm(Recorder(bad)).noul("s", "q")


def test_redaction_applied_before_send() -> None:
    rec = Recorder(chat({"p_yes": 0.1}))
    secret = "api_key=" + "Zx9" * 14
    dm(rec).noul(f"state with {secret}", "q")
    assert "Zx9Zx9Zx9" not in rec.requests[0].content.decode()


# ------------------------------------------------------------- retries / errors
@pytest.mark.parametrize("status", [429, 500, 503])
def test_retries_then_succeeds_honoring_retry_after(status: int) -> None:
    slept: list[float] = []
    rec = Recorder(httpx.Response(status, headers={"retry-after": "1.5"}), chat({"p_yes": 0.4}))
    m = build_aoai_decision_model(CFG, 2.0, transport=httpx.MockTransport(rec), sleep=slept.append)
    assert m.noul("s", "q") == pytest.approx(0.4)
    assert slept == [1.5] and len(rec.requests) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_never_retries_client_errors(status: int) -> None:
    rec = Recorder(httpx.Response(status, text=f"echo of prompt {KEY}"), chat({"p_yes": 0.4}))
    with pytest.raises(DecisionRuntimeError) as ei:
        dm(rec).noul("s", "q")
    assert len(rec.requests) == 1
    assert "echo of prompt" not in str(ei.value) and KEY not in str(ei.value)


def test_retries_exhausted() -> None:
    rec = Recorder(*[httpx.Response(503) for _ in range(3)])
    with pytest.raises(DecisionRuntimeError, match="503"):
        dm(rec).noul("s", "q")
    assert len(rec.requests) == 3


def test_response_cap_and_depth() -> None:
    big = httpx.Response(200, content=b"[" + b"0," * (1 << 20) + b"0]")
    with pytest.raises(DecisionRuntimeError, match="1 MiB"):
        dm(Recorder(big)).noul("s", "q")
    deep = httpx.Response(200, content=b"[" * 30 + b"]" * 30)
    with pytest.raises(DecisionRuntimeError, match="deep"):
        dm(Recorder(deep)).noul("s", "q")


def test_transport_error_and_timeout_curated() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout(f"timeout {KEY}")

    m = build_aoai_decision_model(CFG, 2.0, transport=httpx.MockTransport(boom))
    with pytest.raises(DecisionRuntimeError) as ei:
        m.noul("s", "q")
    assert KEY not in str(ei.value)


def test_key_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    rec = Recorder(httpx.Response(429), httpx.Response(401))
    with pytest.raises(DecisionRuntimeError):
        dm(rec).noul("s", "q")
    assert KEY not in caplog.text and caplog.records
    assert KEY not in repr(CFG)


# ------------------------------------------------------------- embeddings
def _emb_resp(n: int, dim: int = 384) -> httpx.Response:
    data = [
        {"index": i, "object": "embedding", "embedding": [3.0, 4.0] + [0.0] * (dim - 2)}
        for i in range(n)
    ]
    return httpx.Response(200, json={"object": "list", "model": "m", "data": data, "usage": {}})


def test_embed_batches_normalizes_and_shapes() -> None:
    rec = Recorder(_emb_resp(64), _emb_resp(6))
    e = emb(rec)
    assert e.name == "aoai:te3s"
    vecs = e.embed([f"t{i}" for i in range(70)])
    assert len(vecs) == 70 and len(vecs[0]) == 384
    assert vecs[0][:2] == [0.6, 0.8]
    b = rec.body()
    assert str(rec.requests[0].url).endswith("/openai/v1/embeddings")
    assert b["dimensions"] == 384 and b["model"] == "te3s" and len(b["input"]) == 64


def test_embed_wrong_dim_and_400_and_count() -> None:
    with pytest.raises(EmbeddingRuntimeError, match="1536"):
        emb(Recorder(_emb_resp(1, 1536))).embed(["x"])
    with pytest.raises(EmbeddingDimensionError, match="text-embedding-3"):
        emb(Recorder(httpx.Response(400, text="secret prompt"))).embed(["x"])
    with pytest.raises(EmbeddingRuntimeError, match="number"):
        emb(Recorder(_emb_resp(2))).embed(["x"])
    with pytest.raises(EmbeddingRuntimeError, match="HTTP 401"):
        emb(Recorder(httpx.Response(401))).embed(["x"])


@pytest.mark.slow
@pytest.mark.skipif(
    not (os.environ.get("MCPR_AOAI_ENDPOINT") and os.environ.get("MCPR_RUN_SLOW")),
    reason="live AOAI test: set MCPR_RUN_SLOW=1 and MCPR_AOAI_* env",
)
def test_live_aoai() -> None:  # pragma: no cover - optional, never required
    cfg = AoaiSettings.from_env()
    if cfg.chat_deployment:
        r = build_aoai_decision_model(cfg, 30.0).choice(
            "tools: weather, email", "Which fits 'forecast'?", ["weather", "email"]
        )
        assert r.option in ("weather", "email")
    if cfg.embedding_deployment:
        assert len(AoaiEmbeddingBackend(cfg).embed(["hello"])[0]) == 384


# ------------------------------------------------------------- review fixes
@pytest.mark.parametrize(
    "url",
    [
        "https://ａｂｃ.openai.azure.com",
        "https://a⒈evil.openai.azure.com",
        "https://-x.openai.azure.com",
    ],
)
def test_endpoint_rejects_non_ascii_and_bad_labels(url: str) -> None:
    with pytest.raises(AoaiConfigError):
        _validate_aoai_endpoint(url)


def test_non_ascii_key_rejected_without_echo() -> None:
    bad = AoaiSettings(endpoint=CFG.endpoint, api_key="kéy-secret", chat_deployment="d")
    with pytest.raises(AoaiConfigError) as ei:
        AoaiDecisionModel(bad, 1.0)
    assert "kéy-secret" not in str(ei.value)


def test_nan_retry_after_falls_back_to_jitter() -> None:
    slept: list[float] = []
    rec = Recorder(httpx.Response(429, headers={"retry-after": "nan"}), chat({"p_yes": 0.2}))
    m = build_aoai_decision_model(CFG, 2.0, transport=httpx.MockTransport(rec), sleep=slept.append)
    assert m.noul("s", "q") == pytest.approx(0.2)
    assert len(slept) == 1 and 0 < slept[0] <= 8.0


def test_overflowing_numbers_stay_domain_errors() -> None:
    huge = int("9" * 400)
    content = '{"option": "a", "probabilities": [' + str(huge) + ", 1]}"
    resp = httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    r = dm(Recorder(resp)).choice("s", "q", ["a", "b"])
    assert r.probabilities == {"a": 0.75, "b": 0.25}  # argmax-boost fallback
    r2 = dm(Recorder(chat({"option": "b", "probabilities": [1e308, 1e308]}))).choice(
        "s", "q", ["a", "b"]
    )
    assert r2.probabilities == {"a": 0.25, "b": 0.75}
    body = '{"data": [{"index": 0, "embedding": [' + str(huge) + "]}]}"
    with pytest.raises(EmbeddingRuntimeError):
        emb(Recorder(httpx.Response(200, content=body.encode()))).embed(["x"])


def test_embedding_indices_must_be_permutation() -> None:
    resp = _emb_resp(2)
    data = json.loads(resp.content)
    data["data"][1]["index"] = 0
    with pytest.raises(EmbeddingRuntimeError, match="unusable"):
        emb(Recorder(httpx.Response(200, json=data))).embed(["x", "y"])


# ------------------------------------------------------------- total deadline
class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_retry_refused_when_sleep_would_cross_deadline() -> None:
    clock, slept = FakeClock(), []
    rec = Recorder(httpx.Response(503, headers={"retry-after": "5"}), chat({"p_yes": 0.4}))
    m = build_aoai_decision_model(
        CFG, 2.0, transport=httpx.MockTransport(rec), sleep=slept.append, clock=clock
    )
    with pytest.raises(DecisionRuntimeError, match="timed out"):
        m.noul("s", "q")
    assert slept == [] and len(rec.requests) == 1


def test_slow_attempt_consumes_budget_and_remaining_is_the_timeout() -> None:
    clock = FakeClock()
    seen: list[float] = []

    def slow(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        clock.t += 1.5  # slow upstream
        return httpx.Response(503, headers={"retry-after": "0.4"})

    m = build_aoai_decision_model(
        CFG, 2.0, transport=httpx.MockTransport(slow), sleep=lambda s: None, clock=clock
    )
    with pytest.raises(DecisionRuntimeError, match="timed out"):
        m.noul("s", "q")
    # attempt 1 got the full 2.0s; the retry gets only what is left (0.5s);
    # after it the budget is spent and no third attempt is made.
    assert seen == [pytest.approx(2.0), pytest.approx(0.5)]
