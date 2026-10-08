"""Serve N fleet members over streamable HTTP.

    python -m testbed.serve --servers 100 --port-base 8600 [--ports 2] [--tools 1000]

Servers are spread round-robin over ``--ports`` consecutive ports starting
at ``--port-base``; each lives at ``http://HOST:PORT/<name>/mcp`` (many
servers per port keeps a 100-server fleet inside a small port allocation).

Once every listener is up, one line is printed to stdout:

    READY {"<name>": "<url>", ...}

so a parent process can wait for it and read the endpoint map.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.routing import BaseRoute

from testbed.fleet import ServerSpec, generate_fleet
from testbed.servers import build_server


def assign_ports(specs: list[ServerSpec], port_base: int, ports: int) -> dict[str, int]:
    return {s.name: port_base + (i % ports) for i, s in enumerate(specs)}


def endpoint_urls(
    specs: list[ServerSpec], *, host: str = "127.0.0.1", port_base: int, ports: int = 1
) -> dict[str, str]:
    by_port = assign_ports(specs, port_base, ports)
    return {s.name: f"http://{host}:{by_port[s.name]}/{s.name}/mcp" for s in specs}


def build_app(specs: list[ServerSpec], *, host: str = "127.0.0.1") -> Starlette:
    """One Starlette app hosting every spec at ``/<name>/mcp``."""
    servers = [build_server(s) for s in specs]
    routes: list[BaseRoute] = []
    for spec, srv in zip(specs, servers, strict=True):
        sub = srv.streamable_http_app(
            streamable_http_path=f"/{spec.name}/mcp",
            stateless_http=True,
            json_response=True,
            host=host,
        )
        routes.extend(sub.routes)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # Mounted sub-apps' lifespans never run; enter each session manager here.
        async with contextlib.AsyncExitStack() as stack:
            for srv in servers:
                await stack.enter_async_context(srv.session_manager.run())
            yield

    return Starlette(routes=routes, lifespan=lifespan)


async def serve(specs: list[ServerSpec], *, host: str, port_base: int, ports: int) -> None:
    by_port = assign_ports(specs, port_base, ports)
    uvs: list[uvicorn.Server] = []
    for port in sorted(set(by_port.values())):
        group = [s for s in specs if by_port[s.name] == port]
        cfg = uvicorn.Config(
            build_app(group, host=host), host=host, port=port, log_level="warning", lifespan="on"
        )
        uvs.append(uvicorn.Server(cfg))
    tasks = [asyncio.create_task(u.serve()) for u in uvs]
    while not all(u.started for u in uvs):
        if any(t.done() for t in tasks):  # a listener died (e.g. port in use)
            for t in tasks:
                t.cancel()
            raise SystemExit("testbed.serve: a listener failed to start")
        await asyncio.sleep(0.05)
    urls = endpoint_urls(specs, host=host, port_base=port_base, ports=ports)
    sys.stdout.write("READY " + json.dumps(urls) + "\n")
    sys.stdout.flush()
    await asyncio.gather(*tasks)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m testbed.serve")
    ap.add_argument("--servers", type=int, default=10)
    ap.add_argument("--tools", type=int, default=None)
    ap.add_argument("--port-base", type=int, default=8600)
    ap.add_argument("--ports", type=int, default=1)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    for noisy in ("mcp", "httpx", "httpx2", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    specs = generate_fleet(args.servers, tools=args.tools)
    asyncio.run(serve(specs, host=args.host, port_base=args.port_base, ports=max(1, args.ports)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
