"""Run an ASGI app on a real loopback port in a background thread."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import uvicorn

from tests.support.ports import HOST, bound_socket
from tests.support.wait import wait_for


def run_app(app: Any, *, timeout: float = 20.0) -> tuple[int, Callable[[], None]]:
    """Serve ``app`` on an OS-assigned port; return ``(port, stop)``.

    The socket is bound before uvicorn starts and passed in via
    ``Server.serve(sockets=[...])``, so the port can never be taken by another
    test in between. Returns only once uvicorn reports ``started``. ``stop()``
    asks the server to exit, joins the thread and closes the socket; it is
    idempotent."""
    sock = bound_socket(HOST)
    port: int = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="auto"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        wait_for(lambda: server.started or not thread.is_alive(), timeout)
    except TimeoutError:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        raise
    if not server.started:
        sock.close()
        raise RuntimeError("uvicorn exited before it started")

    def stop() -> None:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()

    return port, stop
