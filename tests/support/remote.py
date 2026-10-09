"""Shared helpers moved out of tests/test_backends_remote.py (W0-2); not a test module."""

from __future__ import annotations

from collections.abc import Callable


def _trickle_server(interval: float) -> tuple[str, Callable[[], None]]:
    """A loopback server that trickles response HEADERS one byte per interval."""
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def serve() -> None:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            conn.recv(65536)
            data = b"HTTP/1.1 200 OK\r\nX-Slow: " + b"a" * 10_000
            for b in data:
                if stop.wait(interval):
                    return
                try:
                    conn.sendall(bytes([b]))
                except OSError:
                    return

    threading.Thread(target=serve, daemon=True).start()

    def shutdown() -> None:
        stop.set()
        srv.close()

    return f"http://127.0.0.1:{srv.getsockname()[1]}/v1/decisions", shutdown
