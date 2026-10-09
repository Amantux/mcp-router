"""Argument validation + rate limiter units (no DB)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

import pytest

from mcprouter.execution.ratelimit import SlidingWindowLimiter
from mcprouter.execution.validation import ArgumentValidationError, validate_arguments
from tests.support.ports import bound_socket

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "repo": {"type": "string"},
        "opts": {"type": "object", "properties": {"n": {"type": "integer"}}},
        "items": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["repo"],
    "additionalProperties": False,
}


def _errors(schema: dict[str, Any] | None, args: Any) -> list[str]:
    with pytest.raises(ArgumentValidationError) as ei:
        validate_arguments(schema, args)
    return ei.value.errors


def test_valid_arguments_pass_through() -> None:
    args = {"repo": "a/b", "opts": {"n": 1}, "items": [1, 2]}
    assert validate_arguments(SCHEMA, args) == args


def test_errors_are_paths_and_keywords_only() -> None:
    errs = _errors(SCHEMA, {"repo": 1, "opts": {"n": "s3cret"}, "items": [1, "x9"]})
    assert errs == ["$.items[1]: type", "$.opts.n: type", "$.repo: type"]
    joined = " ".join(errs)
    assert "s3cret" not in joined and "x9" not in joined


def test_missing_required_and_extra_props() -> None:
    errs = _errors(SCHEMA, {"zzz_secret_name": "v"})
    assert sorted(errs) == ["$: additionalProperties", "$: required"]
    assert "zzz_secret_name" not in " ".join(errs)


@pytest.mark.parametrize("args", [None, [], "str", 3])
def test_non_object_arguments_rejected(args: Any) -> None:
    assert _errors(SCHEMA, args) == ["$: arguments must be an object"]


def test_no_schema_accepts_object() -> None:
    assert validate_arguments({}, {"a": 1}) == {"a": 1}
    assert validate_arguments(None, {}) == {}


def test_invalid_schema_fails_closed() -> None:
    assert _errors({"type": "not-a-type"}, {}) == ["tool input schema is invalid"]


def test_error_count_is_capped() -> None:
    schema = {"type": "object", "properties": {f"p{i}": {"type": "integer"} for i in range(30)}}
    errs = _errors(schema, {f"p{i}": "x" for i in range(30)})
    assert len(errs) == 10


@pytest.fixture()
def listener() -> Iterator[tuple[int, list[bytes]]]:
    """A live TCP listener on an OS-assigned port, recording any hit."""
    hits: list[bytes] = []
    srv = bound_socket()
    srv.listen(4)
    srv.settimeout(0.2)
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            hits.append(conn.recv(1024))
            conn.sendall(b'HTTP/1.0 200 OK\r\n\r\n{"type": "object"}')
            conn.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    yield srv.getsockname()[1], hits
    stop.set()
    t.join(1)
    srv.close()


def test_remote_ref_is_never_fetched(listener: tuple[int, list[bytes]]) -> None:
    """SSRF guard: a tool schema's remote $ref must not make us dial out."""
    port, hits = listener
    schema = {"$ref": f"http://127.0.0.1:{port}/evil.json"}
    assert _errors(schema, {}) == ["tool input schema has an unresolvable $ref"]
    assert hits == []


# ------------------------------------------------------------ rate limiter
class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_sliding_window_admits_limit_then_refuses() -> None:
    clock = FakeClock()
    lim = SlidingWindowLimiter(3, 60.0, clock)
    assert [lim.try_acquire("a") for _ in range(4)] == [True, True, True, False]


def test_sliding_window_slides() -> None:
    clock = FakeClock()
    lim = SlidingWindowLimiter(2, 60.0, clock)
    assert lim.try_acquire("a")
    clock.t += 30
    assert lim.try_acquire("a")
    assert not lim.try_acquire("a")
    clock.t += 30.001  # first hit leaves the window
    assert lim.try_acquire("a")
    assert not lim.try_acquire("a")


def test_refused_calls_do_not_consume_budget() -> None:
    clock = FakeClock()
    lim = SlidingWindowLimiter(1, 60.0, clock)
    assert lim.try_acquire("a")
    for _ in range(100):
        assert not lim.try_acquire("a")
    clock.t += 60.001
    assert lim.try_acquire("a")


def test_keys_are_independent() -> None:
    lim = SlidingWindowLimiter(1, 60.0, FakeClock())
    assert lim.try_acquire("a") and lim.try_acquire("b")
    assert not lim.try_acquire("a")


@pytest.mark.parametrize("limit", [0, -5])
def test_non_positive_limit_fails_closed(limit: int) -> None:
    assert not SlidingWindowLimiter(limit).try_acquire("a")


def test_limiter_is_thread_safe() -> None:
    lim = SlidingWindowLimiter(500, 60.0)
    admitted: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(100):
            ok = lim.try_acquire("a")
            with lock:
                admitted.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert admitted.count(True) == 500
