# Wave 3 — remote System One decision backend (executor A)

Not yet wired: engine.py `DECISION_BACKENDS` is still `("deterministic", "laya")`,
so `MCPR_DECISION_BACKEND=remote` is rejected by the engine until the wiring run
adds it.

## Wire contract

Source: https://docs.aimlapi.com/api-references/decision-models/typesafe/jev
(re-fetched 2026-10-08). `POST https://api.aimlapi.com/v1/decisions`,
`Authorization: Bearer <key>`, body `{"model", "state", "questions": {key: {"type",
"instructions", "criteria"}}}`; response `{"model", "answers": {key: ...}, "usage"}`.

Deltas from the earlier brief:
- The response `model` is a versioned id (`typesafe/jev-1.13-20260917`), not the
  requested `typesafe/jev`. We do not compare them.
- `noul` `criteria` is optional; we send none.
- The limit is "32K context" (tokens, not characters). We don't truncate. An
  over-long state comes back as a 4xx, which becomes a typed error and the
  engine falls back.

## Settings (block "# wave-3 remote decision backend" in settings.py)

| env | default | notes |
|---|---|---|
| `MCPR_DECISION_BACKEND` | `deterministic` | new value `remote` |
| `MCPR_DECISION_ENDPOINT` | `https://api.aimlapi.com/v1/decisions` | full URL; with no path (or `/`), `/v1/decisions` is appended (`resolve_endpoint`) |
| `MCPR_DECISION_MODEL` | `typesafe/jev` | backend name is `remote:<model>` |
| `MCPR_DECISION_API_KEY_FILE` | unset | **wins** over `MCPR_DECISION_API_KEY`; trailing CR/LF stripped; unreadable file fails startup |
| `MCPR_DECISION_API_KEY` | unset | `repr=False` on the Settings field |
| `MCPR_DECISION_MAX_RETRIES` | `2` | must be >= 0; applies only to 429/5xx |

An empty string counts as unset, the same as for every other setting. The per-attempt
timeout is the existing `MCPR_DECISION_TIMEOUT_S`.

## Mapping decisions

- choice: `criteria = {option: option}`. The option string is used verbatim as both the key and the
  description, so the returned `choice` is compared against the option list
  directly. Missing probabilities count as 0, then the distribution is renormalized.
- score: `criteria = levels`. `level` = argmax over `probabilities["0".."n-1"]`
  (the first index wins a tie). **The fractional `score` float is ignored**, because the protocol's
  level is an index, and rounding an expectation can pick a level the model
  itself rates unlikely.
- noul: `answer["noul"]` must be finite and in [0,1]. A float overshoot of up to 1e-6 is clamped.
- score_batch: one request with keys `q0..qn-1`. An empty list makes no request.
- The state is passed through `execution.redaction.redact` before it is sent.

## Safety properties

- `urlcheck.validate_outbound_url` runs on **every request** (point of use). It requires https,
  allows http only to localhost/127.0.0.1/::1, and rejects userinfo, whitespace, fragments, and URLs with no host.
- Retries happen only on 429/500/502/503/504. The backoff is jittered exponential (0.25s·2^n) with
  Retry-After (seconds) as a floor. A wait longer than 5s fails fast instead of being slept through. Other 4xx
  responses are never retried, and 401/403 raise `RemoteAuthError`. Timeouts are not retried, which keeps the call
  inside the decision deadline.
- Response bodies over 1 MiB are refused (both a Content-Length pre-check and a streamed cap), as is
  JSON nesting deeper than 20 (a pre-scan before `json.loads`).
- Every error is a `RemoteDecisionError(DecisionRuntimeError)` subclass with a curated
  message, raised `from None` so the httpx exception (and its request headers) isn't chained.
  Upstream bodies are never read into messages. Logs record only the status and a sanitized `x-request-id`.
- The key lives in a name-mangled attribute, `__repr__` omits it, and redirects are
  not followed. `trust_env=False` means env proxies and netrc are ignored.

## Public API for the wiring run

```python
from mcprouter.inference.remote_systemone import RemoteSystemOneModel, resolve_endpoint
m = RemoteSystemOneModel(endpoint=s.decision_endpoint, api_key=s.decision_api_key,
                         model=s.decision_model, timeout_s=s.decision_timeout_s,
                         max_retries=s.decision_max_retries)   # + transport=, sleep= for tests
ValidatedDecisionModel(m)   # always wrap
from mcprouter.inference.urlcheck import validate_outbound_url, InvalidEndpointError
```
