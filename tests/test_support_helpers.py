"""Unit tests for tests/support/{ports,serve,wait}.py (no DB)."""

from __future__ import annotations

import socket

import httpx
import pytest
from fastapi import FastAPI

from tests.support.ports import bound_socket, free_port
from tests.support.serve import run_app
from tests.support.wait import wait_for


def test_free_port_is_bindable() -> None:
    port = free_port()
    assert 0 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_bound_sockets_hold_distinct_ports() -> None:
    a, b = bound_socket(), bound_socket()
    try:
        pa, pb = a.getsockname()[1], b.getsockname()[1]
        assert pa != pb and pa > 0 and pb > 0
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, pytest.raises(OSError):
            s.bind(("127.0.0.1", pa))  # reserved while held
    finally:
        a.close()
        b.close()


def test_wait_for_returns_once_condition_holds() -> None:
    calls = {"n": 0}

    def cond() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    wait_for(cond, timeout=5, interval=0.001)
    assert calls["n"] == 3


def test_wait_for_times_out() -> None:
    with pytest.raises(TimeoutError):
        wait_for(lambda: False, timeout=0.05, interval=0.01)


def test_run_app_serves_on_returned_port_and_stops() -> None:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"pong": "ok"}

    port, stop = run_app(app)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/ping", timeout=5)
        assert r.status_code == 200 and r.json() == {"pong": "ok"}
    finally:
        stop()
    stop()  # idempotent
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"http://127.0.0.1:{port}/ping", timeout=2)
