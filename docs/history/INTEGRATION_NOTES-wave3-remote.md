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
| `MCPR_DECISION_API_KEY_FILE` | unset | **wins** over `MCPR_DECISION_API_KEY`; surrounding whitespace stripped; an unreadable, non-UTF-8 or >64 KiB file fails startup |
| `MCPR_DECISION_API_KEY` | unset | `repr=False` on the Settings field |
| `MCPR_DECISION_MAX_RETRIES` | `2` | must be in [0, 10]; applies only to 429/5xx |

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
  The URL is parsed with `httpx.URL`, so NUL/control bytes, IDNA-invalid hosts and an unbalanced `[::1` are
  refused with a curated message that never echoes the host.
- **Network posture (owner decision).** Link-local and metadata IP literals are always refused, for any
  scheme: `169.254.0.0/16`, `fe80::/10`, `100.100.100.200`, `fd00:ec2::254` plus their alternative encodings: inet_aton
  forms (decimal `2852039166`, hex `0xa9fea9fe`, octal, short-dotted) and IPv6 forms embedding IPv4
  (`::ffff:0:0/96`, `::ffff:0:0:0/96`, `::/96` except `::`/`::1`, NAT64 `64:ff9b::/96`).
  RFC 1918 / ULA private ranges are **allowed**, because a LAN edge router is the intended remote.
  DNS names are **not resolved** at validation time. Residual risk: a DNS name that resolves to a
  link-local or metadata address is not caught here.
- Retries happen only on 429/500/502/503/504. The backoff is jittered exponential (0.25s·2^n) with
  Retry-After (seconds) as a floor. The jittered sleep is capped at 5s, and a Retry-After over 5s fails fast.
  Other 4xx responses are never retried, and 401/403 raise `RemoteAuthError`. Timeouts are not retried.
- **Deadline semantics.** `timeout_s` (`MCPR_DECISION_TIMEOUT_S`) is a **total** budget for one logical call:
  `deadline = monotonic() + timeout_s` is fixed at the start. Each attempt gets the remaining budget as its
  httpx timeout. A retry whose planned sleep would end past the deadline is refused, and the deadline is
  re-checked after every response-body chunk. Any overrun raises `RemoteTimeoutError`.
  Bound: httpx applies the read timeout per read and fixes it when the attempt starts, so one stalled read
  that straddles the deadline can overrun by up to that attempt's remaining budget.
  **Inner bound (hard stop):** httpx timeouts are per socket operation, so a peer trickling response
  *headers* one byte at a time never trips them. Each attempt therefore starts a `threading.Timer(remaining)`
  that closes that attempt's own `httpx.Client` (its connection); the blocked read fails within one trickle
  interval and is reported as `RemoteTimeoutError`. Measured: headers trickled at 0.3 s/byte with
  `timeout_s=0.5` time out in ~0.61 s (3 runs: 0.611/0.614/0.613), i.e. about `timeout_s` + one byte
  interval + ~0.01 s. **Outer bound:** the engine's `DeadlineDecisionModel` still wraps every call.
  The earlier "under 2×`timeout_s`" claim was wrong (trickled headers ran 9 s against `timeout_s=1.0`).
- A probability on a key we did not offer (a choice option or score level) that totals more than 1e-6 is
  refused with `RemoteResponseError`. It is not dropped and renormalized away.
- Response bodies over 1 MiB are refused (both a Content-Length pre-check and a streamed cap), as is
  JSON nesting deeper than 20 (a pre-scan before `json.loads`).
- Every error is a `RemoteDecisionError(DecisionRuntimeError)` subclass with a curated
  message. Transport errors are mapped to a flag inside the `except httpx...` block, and the curated error
  is raised **after** that block. As a result, the raised exception has neither `__cause__` nor `__context__`
  pointing at the httpx exception, whose `.request.headers` holds the Bearer key. A `raise ... from None`
  inside the except block would only hide the context and would still keep it reachable. Tests walk the full chain.
  Upstream bodies are never read into messages. Logs record only the status and a sanitized `x-request-id`.
- The key lives in a name-mangled attribute, `__repr__` omits it, and redirects are
  not followed. `trust_env=False` means env proxies and netrc are ignored.

## Public API for the wiring run

```python
from mcprouter.inference.remote_systemone import RemoteSystemOneModel, resolve_endpoint
m = RemoteSystemOneModel(endpoint=s.decision_endpoint, api_key=s.decision_api_key,
                         model=s.decision_model, timeout_s=s.decision_timeout_s,
                         max_retries=s.decision_max_retries)   # + transport=, sleep=, clock= for tests
ValidatedDecisionModel(m)   # always wrap
from mcprouter.inference.urlcheck import validate_outbound_url, InvalidEndpointError
```

## FIX-2 notes (wave 3)

- API keys must be printable ASCII with no whitespace/control characters (`[\x21-\x7e]`), enforced in
  `Settings` (`MCPR_DECISION_API_KEY[_FILE]`) and in `RemoteSystemOneModel.__init__` (`RemoteKeyFormatError`,
  an `InferenceError` with a curated message). A non-ASCII key would otherwise reach httpx and leak whole via
  `UnicodeEncodeError.object`.
- `Retry-After` / `Content-Length` are parsed only when they match `^[0-9]+$` (`str.isdigit()` accepts `²`).
- A `choice` answer naming an option we did not offer is refused in-backend (`RemoteResponseError`).
- Engine wiring: none required; the hard stop is internal to `_attempt` (one short-lived client per attempt
  when no transport is injected).
