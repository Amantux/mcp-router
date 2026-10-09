"""MT-1 route-hit recorder (wave-6 P-211). Registered via ``pytest_plugins``.

Wraps ``TestClient.request`` (the single choke point: ``.get/.post/...``
delegate to it) and records ``(METHOD, OpenAPI path template, status)`` for
every request a test makes. The template is resolved from the app the client
holds (many tests build partial apps), via ``fastapi.routing.
iter_route_contexts`` in registration order (first match wins, as in routing).

Single process: hits stay in memory and ``tests/test_zz_route_coverage.py``
checks them at the end of the run. Under xdist each worker dumps its hits to
``<rootdir>/.pytest_cache/mcpr_route_hits/<worker>.json`` (git-ignored) and the controller merges them
at ``pytest_sessionfinish`` and runs the same check there.

The coverage check is ENFORCED only with ``MCPR_ENFORCE_ROUTE_COVERAGE=1``
(CI, full runs). Requests made over real sockets (uvicorn + httpx) are
invisible here; every route they reach is also driven through TestClient.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.testclient import TestClient

ENFORCE_ENV = "MCPR_ENFORCE_ROUTE_COVERAGE"
SNAPSHOT = Path(__file__).resolve().parents[2] / "docs" / "reference" / "openapi.json"
HITS_DIRNAME = ".pytest_cache/mcpr_route_hits"
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_CONVERTOR = re.compile(r"\{([^}:]+):[^}]+\}")

# (METHOD, path) -> set of status codes, for this process.
HITS: dict[tuple[str, str], set[int]] = {}

# Routes no test can (or should) drive to a 2xx through TestClient, with the
# reason. Keep it tiny: every entry is a route whose success path is untested.
NEGATIVE_ONLY: dict[tuple[str, str], str] = {}

_real_request = TestClient.request
_TEMPLATES: dict[int, list[tuple[re.Pattern[str], frozenset[str], str]]] = {}


def _templates(app: FastAPI) -> list[tuple[re.Pattern[str], frozenset[str], str]]:
    key = id(app)
    cached = _TEMPLATES.get(key)
    if cached is None:
        cached = []
        for ctx in iter_route_contexts(app.routes):
            if isinstance(ctx.original_route, APIRoute) and ctx.path_regex is not None:
                path = _CONVERTOR.sub(r"{\1}", ctx.path)
                cached.append((ctx.path_regex, frozenset(ctx.methods or ()), path))
        _TEMPLATES[key] = cached
    return cached


def template_for(app: Any, method: str, path: str) -> str | None:
    """The OpenAPI path template `app` would route `method path` to, or None."""
    if not isinstance(app, FastAPI):
        return None
    for regex, methods, template in _templates(app):
        if method in methods and regex.match(path):
            return template
    return None


def _recording_request(self: TestClient, method: str, url: Any, *a: Any, **kw: Any) -> Any:
    resp = _real_request(self, method, url, *a, **kw)
    try:
        m = method.upper()
        tpl = template_for(self.app, m, str(resp.request.url.path))
        if tpl is not None:
            HITS.setdefault((m, tpl), set()).add(resp.status_code)
    except Exception:  # noqa: BLE001 — recording must never change a test's outcome
        pass
    return resp


# --------------------------------------------------------------- the check
def snapshot_routes(spec: Mapping[str, Any] | None = None) -> set[tuple[str, str]]:
    if spec is None:
        spec = json.loads(SNAPSHOT.read_text())
    return {
        (m.upper(), p) for p, ops in spec["paths"].items() for m in ops if m.upper() in _METHODS
    }


def coverage_failures(
    hits: Mapping[tuple[str, str], Iterable[int]],
    routes: set[tuple[str, str]],
    negative_only: Mapping[tuple[str, str], str] = NEGATIVE_ONLY,
) -> list[str]:
    """Human-readable problems: routes never requested, routes never 2xx
    (outside `negative_only`), and stale `negative_only` entries."""
    out: list[str] = []
    for key in sorted(routes):
        codes = set(hits.get(key, ()))
        label = f"{key[0]} {key[1]}"
        if not codes:
            out.append(f"never requested: {label}")
        elif not any(200 <= c < 300 for c in codes) and key not in negative_only:
            out.append(f"never 2xx: {label} (codes {sorted(codes)})")
    for key in sorted(negative_only):
        if key not in routes:
            out.append(f"NEGATIVE_ONLY lists a route that no longer exists: {key}")
        elif any(200 <= c < 300 for c in hits.get(key, ())):
            out.append(f"NEGATIVE_ONLY entry now gets a 2xx; remove it: {key}")
    return out


def summary(hits: Mapping[tuple[str, str], Iterable[int]], routes: set[tuple[str, str]]) -> str:
    requested = sum(1 for k in routes if hits.get(k))
    ok = sum(1 for k in routes if any(200 <= c < 300 for c in hits.get(k, ())))
    return f"route coverage: {requested}/{len(routes)} requested, {ok}/{len(routes)} with a 2xx"


def enforced() -> bool:
    return os.environ.get(ENFORCE_ENV) == "1"


# ----------------------------------------------------------- pytest hooks
def _hits_dir(config: pytest.Config) -> Path:
    return Path(str(config.rootpath)) / HITS_DIRNAME


def _dump(hits: Mapping[tuple[str, str], set[int]]) -> dict[str, list[int]]:
    return {f"{m} {p}": sorted(c) for (m, p), c in hits.items()}


def _load(raw: Mapping[str, list[int]]) -> dict[tuple[str, str], set[int]]:
    out: dict[tuple[str, str], set[int]] = {}
    for k, codes in raw.items():
        m, _, p = k.partition(" ")
        out.setdefault((m, p), set()).update(codes)
    return out


def pytest_configure(config: pytest.Config) -> None:
    TestClient.request = _recording_request  # type: ignore[method-assign]
    if not hasattr(config, "workerinput"):  # controller / single process: start clean
        d = _hits_dir(config)
        if d.is_dir():
            for f in d.glob("*.json"):
                f.unlink()


def pytest_unconfigure(config: pytest.Config) -> None:
    TestClient.request = _real_request  # type: ignore[method-assign]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    workerinput = getattr(config, "workerinput", None)
    if workerinput is not None:  # xdist worker: hand hits to the controller
        d = _hits_dir(config)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{workerinput['workerid']}.json").write_text(json.dumps(_dump(HITS)))
        return
    d = _hits_dir(config)
    worker_files = sorted(d.glob("*.json")) if d.is_dir() else []
    merged: dict[tuple[str, str], set[int]] = {k: set(v) for k, v in HITS.items()}
    for f in worker_files:
        for k, codes in _load(json.loads(f.read_text())).items():
            merged.setdefault(k, set()).update(codes)
    if not merged or not SNAPSHOT.is_file():
        return
    routes = snapshot_routes()
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None and enforced():
        reporter.write_line(summary(merged, routes))
    # Single process: tests/test_zz_route_coverage.py already enforced in-process.
    if worker_files and enforced() and exitstatus == 0:
        problems = coverage_failures(merged, routes)
        if problems:
            if reporter is not None:
                reporter.write_line("MT-1 route coverage FAILED:\n  " + "\n  ".join(problems))
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
