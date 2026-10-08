"""Test harness helpers: launch fleet members as real processes.

Every process started here is terminated in a ``finally`` — callers use the
context managers and never manage PIDs themselves.
"""

from __future__ import annotations

import json
import os
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
    port_base: int,
    ports: int = 1,
    tools: int | None = None,
    startup_timeout_s: float = 60.0,
) -> Iterator[dict[str, str]]:
    """Run ``python -m testbed.serve`` and yield ``{server name: url}``."""
    cmd = [sys.executable, "-m", "testbed.serve", "--servers", str(n_servers)]
    cmd += ["--port-base", str(port_base), "--ports", str(ports)]
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

    DEAD_URL = "http://127.0.0.1:8619/dead/mcp"  # nothing listens on 8619 in tests

    def __init__(self, specs: list[ServerSpec] | None = None) -> None:
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
            dead = ServerTarget(transport="streamable-http", endpoint=self.DEAD_URL)
            return Connector(dead, connect_timeout_s=3)
        return Connector(build_server(self.specs[server.name]))
