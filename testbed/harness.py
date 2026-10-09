"""Test harness helpers: launch fleet members as real processes.

Every process started here is terminated in a ``finally`` — callers use the
context managers and never manage PIDs themselves.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from testbed.fleet import ServerSpec

REPO_ROOT = Path(__file__).resolve().parent.parent


def stdio_env() -> dict[str, str]:
    """Env a stdio fleet member needs (the SDK passes only a minimal default env)."""
    return {"PYTHONPATH": str(REPO_ROOT)}


def stdio_command_for_index(n_servers: int, index: int, *, tools: int | None = None) -> list[str]:
    cmd = [sys.executable, "-m", "testbed.stdio_server", "--servers", str(n_servers)]
    cmd += ["--index", str(index)]
    if tools is not None:
        cmd += ["--tools", str(tools)]
    return cmd


def write_spec_file(spec: ServerSpec, path: Path) -> Path:
    path.write_text(json.dumps(asdict(spec)))
    return path


def stdio_command_for_spec_file(path: Path) -> list[str]:
    return [sys.executable, "-m", "testbed.stdio_server", "--spec-file", str(path)]


def _terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@contextmanager
def http_fleet(
    n_servers: int,
    *,
    port_base: int | None = None,
    ports: int = 1,
    tools: int | None = None,
    startup_timeout_s: float = 60.0,
) -> Iterator[dict[str, str]]:
    """Run ``python -m testbed.serve`` and yield ``{server name: url}``.

    ``port_base=None`` (default): OS-assigned free ports, read back from READY."""
    cmd = [sys.executable, "-m", "testbed.serve", "--servers", str(n_servers)]
    cmd += ["--ports", str(ports)]
    if port_base is not None:
        cmd += ["--port-base", str(port_base)]
    if tools is not None:
        cmd += ["--tools", str(tools)]
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )
    try:
        assert proc.stdout is not None
        found: list[str] = []

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                if line.startswith("READY "):
                    found.append(line[len("READY ") :])
                    return

        th = threading.Thread(target=_reader, daemon=True)
        th.start()
        deadline = time.monotonic() + startup_timeout_s
        while not found:
            if proc.poll() is not None:
                raise RuntimeError(f"testbed.serve exited early (code {proc.returncode})")
            if time.monotonic() > deadline:
                raise RuntimeError("testbed.serve did not become ready in time")
            time.sleep(0.05)
        urls: dict[str, str] = json.loads(found[0])
        yield urls
    finally:
        _terminate(proc)


class InprocFleet:
    """Connector factory serving registered servers from in-process MCPServers.

    Mutate ``specs[name]`` between syncs to simulate a server changing; put a
    name in ``down`` to make it unreachable (points at a dead local port).
    """

    def __init__(self, specs: list[ServerSpec] | None = None) -> None:
        self.dead_url = f"http://127.0.0.1:{dead_port()}/dead/mcp"
        self.specs: dict[str, ServerSpec] = {s.name: s for s in specs or []}
        self.down: set[str] = set()

    def endpoint(self, name: str) -> str:
        # Placeholder endpoint for the registration row; never dialled.
        return f"http://inproc.invalid/{name}/mcp"

    def factory(self, server: Any, target: Any) -> Any:
        from mcprouter.mcpclient import Connector, ServerTarget
        from testbed.servers import build_server

        del target
        if server.name in self.down:
            dead = ServerTarget(transport="streamable-http", endpoint=self.dead_url)
            return Connector(dead, connect_timeout_s=3)
        return Connector(build_server(self.specs[server.name]))


def dead_port(host: str = "127.0.0.1") -> int:
    """A port nothing listens on: bound by us, then closed (never a fixed number)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        port: int = sock.getsockname()[1]
        return port


@contextmanager
def sse_server(spec: ServerSpec, *, port: int = 0, host: str = "127.0.0.1") -> Iterator[str]:
    """Serve one spec over LEGACY SSE in a background thread; yield its URL.

    ``port=0`` (default): a pre-bound OS-assigned port handed to uvicorn."""
    import uvicorn

    from testbed.servers import build_server

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(build_server(spec).sse_app(host=host), log_level="warning")
    )
    th = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    th.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not th.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("sse server did not start")
            time.sleep(0.02)
        yield f"http://{host}:{port}/sse"
    finally:
        server.should_exit = True
        th.join(timeout=10)
        sock.close()
