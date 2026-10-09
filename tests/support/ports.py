"""Ephemeral loopback ports, so tests never collide on a fixed number."""

from __future__ import annotations

import socket

HOST = "127.0.0.1"


def bound_socket(host: str = HOST) -> socket.socket:
    """A listening-ready TCP socket bound to an OS-assigned port on ``host``.

    Hand it straight to a server (``uvicorn.Server.serve(sockets=[sock])``):
    the port is reserved from the moment of binding, so there is no
    probe-then-bind race."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    return sock


def free_port(host: str = HOST) -> int:
    """A port that was free a moment ago. Only for consumers that must be
    given a number (a child process, a URL in config); prefer
    :func:`bound_socket` when you control the server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        port: int = sock.getsockname()[1]
        return port
