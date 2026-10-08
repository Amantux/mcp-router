"""Register + discover a synthetic fleet straight into the database.

    python -m testbed.seed --servers 100 --tools 1000                 # in-process
    python -m testbed.seed --servers 100 --tools 1000 --transport http --port-base 8600 --ports 2

``inproc`` (default) discovers each server through an in-process MCPServer
(no network; rows keep a placeholder ``http://inproc.invalid/...`` endpoint,
so later refreshes of those rows fail as unreachable — they exist to give
the catalog realistic content for scale tests). ``http`` launches
``testbed.serve`` for the duration of the run and discovers over real
streamable HTTP. Existing servers with the same name are reused.

Prints one JSON summary line: servers, tools, failures, timings.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import anyio
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.db import init_db, make_engine, make_session_factory
from mcprouter.discovery import (
    DiscoveryService,
    DuplicateServerError,
    ServerRegistration,
    register_server,
)
from mcprouter.discovery.sync import ConnectorFactory, SyncReport, default_connector_factory
from mcprouter.models import MCPServerRecord, MCPToolRecord
from mcprouter.settings import Settings
from testbed.fleet import ServerSpec, generate_fleet
from testbed.harness import InprocFleet, http_fleet


def is_testbed_endpoint(url: str | None, name: str) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    return parts.path == f"/{name}/mcp" and parts.hostname in ("127.0.0.1", "inproc.invalid")


def register_fleet(
    factory: sessionmaker[Session], specs: list[ServerSpec], endpoint: Callable[[str], str]
) -> list[str]:
    """Register every spec as a streamable-http server; return server ids."""
    ids: list[str] = []
    with factory() as s, s.begin():
        for spec in specs:
            try:
                with s.begin_nested():
                    rec = register_server(
                        s,
                        ServerRegistration(
                            name=spec.name,
                            transport="streamable-http",
                            endpoint=endpoint(spec.name),
                        ),
                    )
                ids.append(rec.id)
            except DuplicateServerError:
                existing = s.scalar(
                    select(MCPServerRecord).where(MCPServerRecord.name == spec.name)
                )
                assert existing is not None
                if not is_testbed_endpoint(existing.endpoint, spec.name):
                    # A real server with a colliding name ("github"...): never
                    # repoint it at the testbed and wipe its catalog.
                    raise SystemExit(
                        f"refusing to reuse non-testbed server {spec.name!r}; seed an empty database"
                    ) from None
                existing.endpoint = endpoint(spec.name)
                ids.append(existing.id)
    return ids


def discover(
    factory: sessionmaker[Session],
    ids: list[str],
    *,
    connector_factory: ConnectorFactory = default_connector_factory,
    concurrency: int = 16,
) -> tuple[dict[str, object], float]:
    svc = DiscoveryService(factory, connector_factory=connector_factory, concurrency=concurrency)
    t0 = time.perf_counter()
    results = anyio.run(lambda: svc.sync_all(server_ids=ids))
    return dict(results), time.perf_counter() - t0


def available_tools(factory: sessionmaker[Session], ids: list[str]) -> int:
    with factory() as s:
        return int(
            s.scalar(
                select(func.count())
                .select_from(MCPToolRecord)
                .where(MCPToolRecord.server_id.in_(ids), MCPToolRecord.available.is_(True))
            )
            or 0
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m testbed.seed")
    ap.add_argument("--servers", type=int, default=100)
    ap.add_argument("--tools", type=int, default=1000)
    ap.add_argument("--transport", choices=("inproc", "http"), default="inproc")
    ap.add_argument("--port-base", type=int, default=8600)
    ap.add_argument("--ports", type=int, default=2)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--database-url", default=None, help="defaults to MCPR_DATABASE_URL")
    args = ap.parse_args(argv)

    # No silent fallback to the app's default database: seeding is explicit.
    url = args.database_url or os.environ.get("MCPR_DATABASE_URL", "").strip()
    if not url:
        ap.error("set --database-url or MCPR_DATABASE_URL (no default for seeding)")
    settings = Settings(database_url=url)
    engine = make_engine(settings)
    init_db(engine)
    factory = make_session_factory(engine)
    specs = generate_fleet(args.servers, tools=args.tools)

    t0 = time.perf_counter()
    if args.transport == "inproc":
        fleet = InprocFleet(specs)
        ids = register_fleet(factory, specs, fleet.endpoint)
        register_s = time.perf_counter() - t0
        results, refresh_s = discover(
            factory, ids, connector_factory=fleet.factory, concurrency=args.concurrency
        )
    else:
        with http_fleet(
            args.servers, port_base=args.port_base, ports=args.ports, tools=args.tools
        ) as urls:
            ids = register_fleet(factory, specs, urls.__getitem__)
            register_s = time.perf_counter() - t0
            results, refresh_s = discover(factory, ids, concurrency=args.concurrency)

    failures = sum(1 for r in results.values() if not isinstance(r, SyncReport))
    summary = {
        "transport": args.transport,
        "servers": len(ids),
        "tools": available_tools(factory, ids),
        "failures": failures,
        "register_s": round(register_s, 3),
        "refresh_s": round(refresh_s, 3),
    }
    sys.stdout.write(json.dumps(summary) + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
