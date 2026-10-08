"""Remote "System One" decision backend (AIML API typesafe/jev wire shape).

Wire contract (https://docs.aimlapi.com/api-references/decision-models/typesafe/jev):
POST <endpoint> with ``Authorization: Bearer <key>`` and
``{"model", "state", "questions": {key: {"type", "instructions", "criteria"}}}``;
the response carries ``{"model", "answers": {key: ...}, "usage"}``.

Mapping decisions (documented in docs/INTEGRATION_NOTES-wave3-remote.md):
- choice: criteria = {option: option} -- the option string is both key and
  description, so the answer key IS the option verbatim.
- score: criteria = the ordered levels list; ScoreResult.level is the argmax of
  the per-level probabilities ("0".."n-1"). The fractional ``score`` float is
  deliberately ignored: the protocol's level is an index, not an expectation.
- noul: the ``noul`` float, which must be in [0, 1] (tiny overshoot clamped).
- score_batch: ONE request carrying every question.

Wrap instances in ValidatedDecisionModel (the engine does) so a novel option or
malformed distribution is refused before it reaches routing.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from mcprouter.execution.redaction import redact
from mcprouter.inference.errors import DecisionRuntimeError, InferenceError
from mcprouter.inference.urlcheck import validate_outbound_url
from mcprouter.interfaces import ChoiceResult, ScoreResult

log = logging.getLogger(__name__)

DEFAULT_PATH = "/v1/decisions"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 20
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_BACKOFF_BASE_S = 0.25
_MAX_RETRY_SLEEP_S = 5.0  # a longer Retry-After is not waited out: we fail fast
_FOREIGN_MASS_TOLERANCE = 1e-6
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_KEY_RE = re.compile(r"[\x21-\x7e]+")  # printable ASCII, no whitespace/control
# str.isdigit() accepts '\xb2' etc., which int()/float() reject: ASCII digits only.
# Capped length: int() refuses > 4300 digits, and 15 digits already means "huge".
_DIGITS_RE = re.compile(r"[0-9]{1,15}")
_ANY_DIGITS_RE = re.compile(r"[0-9]+")


class RemoteKeyFormatError(InferenceError):
    """The configured API key is not printable ASCII. Curated message: it
    never contains the key (a UnicodeEncodeError's ``.object`` would)."""


class RemoteDecisionError(DecisionRuntimeError):
    """Remote decision call failed. Subclass of DecisionRuntimeError so the
    engine falls back to the deterministic model. Messages are curated."""


class RemoteAuthError(RemoteDecisionError):
    """The remote rejected the credentials (401/403) or none are configured."""


class RemoteRateLimitedError(RemoteDecisionError):
    """429/5xx persisted after the bounded retries."""


class RemoteResponseError(RemoteDecisionError):
    """The response was oversized, too deep, not JSON, or not the documented shape."""


class RemoteTimeoutError(RemoteDecisionError):
    """The request exceeded the decision timeout."""


def resolve_endpoint(endpoint: str) -> str:
    """Append the default ``/v1/decisions`` path when the URL has none."""
    parts = urlsplit(endpoint)
    if parts.path in ("", "/"):
        return urlunsplit(parts._replace(path=DEFAULT_PATH))
    return endpoint


def _check_depth(raw: bytes) -> None:
    """Refuse JSON nested deeper than MAX_JSON_DEPTH before parsing it."""
    depth = 0
    in_str = esc = False
    for b in raw:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:  # backslash
                esc = True
            elif b == 0x22:
                in_str = False
        elif b == 0x22:
            in_str = True
        elif b in (0x5B, 0x7B):
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise RemoteResponseError("remote decision response is nested too deeply")
        elif b in (0x5D, 0x7D):
            depth -= 1


def _prob(v: object) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise RemoteResponseError("remote decision response has an invalid probability")
    return float(v)


def _normalize(ps: list[float]) -> list[float]:
    total = sum(ps)
    if total <= 0:
        raise RemoteResponseError("remote decision response has an empty distribution")
    return [p / total for p in ps]


def _refuse_foreign_mass(raw: dict[Any, Any], allowed: set[str]) -> None:
    """Mass on keys we did not offer must not be silently dropped
    (renormalizing would turn a 2% answer into a 50% one)."""
    foreign = sum(_prob(v) for k, v in raw.items() if k not in allowed)
    if foreign > _FOREIGN_MASS_TOLERANCE:
        raise RemoteResponseError("remote decision response puts probability on unknown options")


class RemoteSystemOneModel:
    """DecisionModel + BatchScoringDecisionModel over the remote HTTP API."""

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str = "typesafe/jev",
        timeout_s: float = 2.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if api_key and not _KEY_RE.fullmatch(api_key):
            raise RemoteKeyFormatError(
                "remote decision API key must be printable ASCII with no whitespace"
            )
        self._endpoint = resolve_endpoint(endpoint)
        self.__api_key = api_key
        self._transport = transport
        self.model = model
        self.name = f"remote:{model}"
        self._max_retries = max(0, max_retries)
        self._sleep = sleep
        self._clock = clock
        self._timeout_s = timeout_s
        self._client = httpx.Client(
            timeout=timeout_s, transport=transport, follow_redirects=False, trust_env=False
        )

    def __repr__(self) -> str:
        return f"RemoteSystemOneModel(name={self.name!r})"

    def close(self) -> None:
        self._client.close()

    # ---------------------------------------------------------------- protocol
    def choice(self, state: str, question: str, options: list[str]) -> ChoiceResult:
        q = {"type": "choice", "instructions": question, "criteria": {o: o for o in options}}
        a = self._ask(state, {"q0": q})["q0"]
        picked = a.get("choice")
        raw = a.get("probabilities")
        if not isinstance(picked, str) or not isinstance(raw, dict) or picked not in options:
            raise RemoteResponseError("remote decision response has a malformed choice answer")
        _refuse_foreign_mass(raw, set(options))
        ps = _normalize([_prob(raw.get(o, 0.0)) for o in options])
        return ChoiceResult(option=picked, probabilities=dict(zip(options, ps, strict=True)))

    def score(self, state: str, question: str, levels: list[str]) -> ScoreResult:
        return self.score_batch(state, [question], levels)[0]

    def score_batch(self, state: str, questions: list[str], levels: list[str]) -> list[ScoreResult]:
        if not questions:
            return []
        qs = {
            f"q{i}": {"type": "score", "instructions": q, "criteria": list(levels)}
            for i, q in enumerate(questions)
        }
        answers = self._ask(state, qs)
        return [self._score_from(answers[f"q{i}"], len(levels)) for i in range(len(questions))]

    def noul(self, state: str, question: str) -> float:
        a = self._ask(state, {"q0": {"type": "noul", "instructions": question}})["q0"]
        p = a.get("noul")
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p):
            raise RemoteResponseError("remote decision response has a malformed noul answer")
        if p < -1e-6 or p > 1 + 1e-6:
            raise RemoteResponseError("remote decision response has a noul outside [0, 1]")
        return min(1.0, max(0.0, float(p)))

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _score_from(a: dict[str, Any], n: int) -> ScoreResult:
        raw = a.get("probabilities")
        if not isinstance(raw, dict):
            raise RemoteResponseError("remote decision response has a malformed score answer")
        _refuse_foreign_mass(raw, {str(i) for i in range(n)})
        ps = _normalize([_prob(raw.get(str(i), 0.0)) for i in range(n)])
        level = max(range(n), key=lambda i: ps[i])  # first index wins a tie
        return ScoreResult(level=level, probabilities=ps)

    def _ask(self, state: str, questions: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        body = {"model": self.model, "state": redact(state), "questions": questions}
        data = self._post(body)
        answers = data.get("answers") if isinstance(data, dict) else None
        if not isinstance(answers, dict):
            raise RemoteResponseError("remote decision response has no answers")
        out: dict[str, dict[str, Any]] = {}
        for key, q in questions.items():
            a = answers.get(key)
            if not isinstance(a, dict) or a.get("type") != q["type"]:
                raise RemoteResponseError("remote decision response is missing an answer")
            out[key] = a
        return out

    def _post(self, body: dict[str, Any]) -> Any:
        """One logical call under a TOTAL deadline of ``timeout_s`` covering
        every attempt and retry sleep. Each attempt gets the remaining budget
        as its httpx timeout; the body read re-checks the deadline per chunk.

        Errors are raised OUTSIDE any ``except httpx...`` block, so no raised
        exception carries an httpx exception (whose ``.request.headers`` holds
        the Bearer key) as ``__context__``/``__cause__``."""
        url = validate_outbound_url(self._endpoint)  # point of use, every call
        if not self.__api_key:
            raise RemoteAuthError("remote decision backend has no API key configured")
        if not _KEY_RE.fullmatch(self.__api_key):
            raise RemoteKeyFormatError(
                "remote decision API key must be printable ASCII with no whitespace"
            )
        deadline = self._clock() + self._timeout_s
        payload = json.dumps(body).encode()
        attempt = 0
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise RemoteTimeoutError("remote decision call timed out")
            outcome = self._attempt(url, payload, remaining, deadline)
            if isinstance(outcome, bytes):
                raw = outcome
                break
            status, retry_after = outcome
            if status in (401, 403):
                raise RemoteAuthError("remote decision service rejected the credentials")
            if status not in _RETRY_STATUSES:
                raise RemoteDecisionError(f"remote decision service returned HTTP {status}")
            delay = self._retry_delay(attempt, retry_after)
            if attempt >= self._max_retries or delay is None:
                raise RemoteRateLimitedError(
                    f"remote decision service unavailable (HTTP {status}) after retries"
                )
            if self._clock() + delay > deadline:
                raise RemoteTimeoutError("remote decision call timed out")
            self._sleep(delay)
            attempt += 1
        _check_depth(raw)
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            raise RemoteResponseError("remote decision response is not valid JSON") from None

    def _attempt(
        self, url: str, payload: bytes, remaining: float, deadline: float
    ) -> bytes | tuple[int, str]:
        """One HTTP attempt: the 200 body, or (status, retry-after) to classify.

        Hard stop (inner bound): httpx's timeout is per socket operation, so a
        peer trickling response HEADERS one byte at a time never trips it. A
        ``threading.Timer(remaining)`` closes this attempt's own client (and so
        its connection) when the budget runs out; the blocked read then fails
        within one trickle interval and is reported as a timeout. The engine's
        DeadlineDecisionModel stays the outer bound. With an injected transport
        (tests) the shared client is used and the timer closes the response.

        An httpx failure only sets a flag inside ``except``; the curated error
        is raised after the block, so nothing chains the httpx exception."""
        failure = ""
        fired = threading.Event()
        own = self._transport is None
        client = (
            httpx.Client(timeout=remaining, follow_redirects=False, trust_env=False)
            if own
            else self._client
        )
        holder: list[httpx.Response] = []

        def hard_stop() -> None:
            fired.set()
            if own:
                client.close()
            for r in holder:
                r.close()

        timer = threading.Timer(remaining, hard_stop)
        timer.daemon = True
        timer.start()
        try:
            resp = client.send(
                client.build_request(
                    "POST",
                    url,
                    content=payload,
                    # built inline: no frame local holds the key for error reporters
                    headers={
                        "Authorization": "Bearer " + self.__api_key,
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    timeout=remaining,
                ),
                stream=True,
            )
            holder.append(resp)
            try:
                status = resp.status_code
                rid = resp.headers.get("x-request-id", "")
                rid = rid if _REQUEST_ID_RE.fullmatch(rid) else "-"
                if status == 200:
                    return self._read_capped(resp, deadline)
                log.warning("remote decision call failed: status=%d request_id=%s", status, rid)
                return status, resp.headers.get("retry-after", "")
            finally:
                resp.close()
        except httpx.TimeoutException:
            failure = "timeout"
        except (httpx.HTTPError, OSError):
            failure = "timeout" if fired.is_set() else "unreachable"
        except RuntimeError:
            if not fired.is_set():
                raise
            failure = "timeout"  # hard stop closed the client before send()
        finally:
            timer.cancel()
            if own:
                client.close()
        if failure == "timeout":
            raise RemoteTimeoutError("remote decision call timed out")
        raise RemoteDecisionError("remote decision service is unreachable")

    def _read_capped(self, resp: httpx.Response, deadline: float) -> bytes:
        declared = resp.headers.get("content-length", "")
        if _ANY_DIGITS_RE.fullmatch(declared) and (
            not _DIGITS_RE.fullmatch(declared) or int(declared) > MAX_RESPONSE_BYTES
        ):
            raise RemoteResponseError("remote decision response is too large")
        buf = bytearray()
        for chunk in resp.iter_bytes():
            buf += chunk
            if len(buf) > MAX_RESPONSE_BYTES:
                raise RemoteResponseError("remote decision response is too large")
            if self._clock() > deadline:
                raise RemoteTimeoutError("remote decision call timed out")
        return bytes(buf)

    @staticmethod
    def _retry_delay(attempt: int, retry_after: str) -> float | None:
        """Jittered exponential backoff; Retry-After (seconds) honored as a floor.
        The jittered delay is capped at _MAX_RETRY_SLEEP_S. None = the server
        asked us to wait longer than we are willing to."""
        delay = _BACKOFF_BASE_S * (2**attempt)
        ra = retry_after.strip()
        if _ANY_DIGITS_RE.fullmatch(ra):
            if not _DIGITS_RE.fullmatch(ra):
                return None  # absurdly long: longer than we will ever wait
            delay = max(delay, float(ra))
        if delay > _MAX_RETRY_SLEEP_S:
            return None
        jittered = delay + random.uniform(0, delay / 2)  # noqa: S311 - jitter, not crypto
        return float(min(_MAX_RETRY_SLEEP_S, jittered))
