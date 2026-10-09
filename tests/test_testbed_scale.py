"""SPEC §3 target: catalog refresh < 60s for 100 servers / 1,000+ tools.

Measured over REAL streamable HTTP (testbed.serve subprocess, 2 ports),
through the production Connector + DiscoveryService + PostgreSQL path —
not in-process shortcuts. Dev-box numbers; the target laptop is validated
separately (docs/hardware-validation.md).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from sqlalchemy.orm import Session, sessionmaker
from testbed.fleet import generate_fleet
from testbed.harness import REPO_ROOT, http_fleet
from testbed.seed import available_tools, discover, register_fleet

from mcprouter.discovery import SyncReport

from .conftest import TEST_DB_URL, requires_db

SF = sessionmaker[Session]
N_SERVERS, N_TOOLS = 100, 1000
REFRESH_BUDGET_S = 60.0


@requires_db
def test_full_catalog_refresh_100_servers_1000_tools_under_60s(db: SF) -> None:
    specs = generate_fleet(N_SERVERS, tools=N_TOOLS)
    with http_fleet(N_SERVERS, ports=2, tools=N_TOOLS) as urls:
        ids = register_fleet(db, specs, urls.__getitem__)

        results, cold_s = discover(db, ids, concurrency=16)  # first discovery: 1000 inserts
        assert all(isinstance(r, SyncReport) for r in results.values()), [
            getattr(r, "message", r) for r in results.values() if not isinstance(r, SyncReport)
        ]
        assert available_tools(db, ids) == N_TOOLS

        results, warm_s = discover(db, ids, concurrency=16)  # steady-state refresh
        assert all(isinstance(r, SyncReport) and not r.changed for r in results.values())

    print(
        f"\ncatalog refresh {N_SERVERS} servers/{N_TOOLS} tools: cold={cold_s:.2f}s warm={warm_s:.2f}s"
    )
    assert cold_s < REFRESH_BUDGET_S
    assert warm_s < REFRESH_BUDGET_S


@requires_db
def test_seed_cli_inproc(db: SF) -> None:
    del db
    t0 = time.perf_counter()
    out = subprocess.run(
        [sys.executable, "-m", "testbed.seed", "--servers", "20", "--tools", "200"],
        cwd=REPO_ROOT,
        env={**os.environ, "MCPR_DATABASE_URL": TEST_DB_URL, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    summary = json.loads(out.stdout.strip().splitlines()[-1])
    assert summary["servers"] == 20 and summary["tools"] == 200 and summary["failures"] == 0
    assert time.perf_counter() - t0 < 120
