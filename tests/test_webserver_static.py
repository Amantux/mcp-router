"""Dashboard static serving, SPA fallback, headers, and MCPR_ALLOWED_HOSTS."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcprouter.api.static import mount_ui
from mcprouter.gateway.server import gateway_transport_security
from mcprouter.settings import _host_list
from tests.edge_app_helpers import AUTH, edge_client


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<html>SPA</html>")
    (d / "assets" / "app-abc123.js").write_text("console.log(1)")
    (d / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("TOP-SECRET")
    return d


def _client(dist: Path) -> TestClient:
    app = FastAPI()

    @app.get("/api/v1/ping")
    def ping() -> dict[str, str]:
        return {"ok": "yes"}

    mount_ui(app, str(dist))
    return TestClient(app)


def test_index_and_spa_fallback_no_store(dist: Path) -> None:
    c = _client(dist)
    for path in ("/", "/servers", "/setup/step/2", "/nope.js"):
        r = c.get(path)
        assert r.status_code == 200 and "SPA" in r.text, path
        assert r.headers["cache-control"] == "no-store"


def test_hashed_assets_immutable(dist: Path) -> None:
    r = _client(dist).get("/assets/app-abc123.js")
    assert r.status_code == 200 and "console" in r.text
    assert "immutable" in r.headers["cache-control"]
    assert _client(dist).get("/favicon.svg").headers["cache-control"] == "no-cache"


def test_security_headers(dist: Path) -> None:
    r = _client(dist).get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    "path", ["/..%2fsecret.txt", "/assets/..%2f..%2fsecret.txt", "/%2e%2e/secret.txt"]
)
def test_traversal_never_escapes_dist(dist: Path, path: str) -> None:
    r = _client(dist).get(path)
    assert "TOP-SECRET" not in r.text


def test_reserved_prefixes_404_not_spa(dist: Path) -> None:
    c = _client(dist)
    assert c.get("/api/v1/ping").json() == {"ok": "yes"}
    for path in ("/api/v1/does-not-exist", "/mcp/x", "/metrics/x", "/healthz/x", "/docs/x"):
        r = c.get(path)
        assert r.status_code == 404 and "SPA" not in r.text, path
    for path in ("/API/x", "/Mcp/x", "/HEALTHZ/x"):  # case must not reach the SPA
        r = c.get(path)
        assert r.status_code == 404 and r.headers["content-type"].startswith("application/json"), (
            path
        )


def test_not_built_page(tmp_path: Path) -> None:
    r = _client(tmp_path / "missing").get("/")
    assert r.status_code == 200 and "npm run build" in r.text


def test_full_app_api_and_mcp_unaffected(dist: Path) -> None:
    with edge_client(ui_dist=str(dist)) as (_app, c):
        assert c.get("/healthz").json() == {"status": "ok"}
        assert c.get("/").text == "<html>SPA</html>"
        assert c.get("/api/v1/nonexistent").status_code == 404
        # body limit still outermost: an oversized POST to an SPA path is refused
        r = c.post("/servers", content=b"x" * (64 * 1024 * 1024))
        assert r.status_code == 413


def test_host_list_parsing() -> None:
    assert _host_list(" Router.LAN , 10.0.0.5:8400,,[::1]:*") == (
        "router.lan",
        "10.0.0.5:8400",
        "[::1]:*",
    )
    for bad in ("*", "evil.com\r\nX: y", "a b", "host:abc", "http://x"):
        with pytest.raises(ValueError):
            _host_list(bad)


def _mcp_status(host: str, allowed: tuple[str, ...]) -> int:
    with edge_client(allowed_hosts=allowed) as (_app, c):
        r = c.post(
            "/mcp",
            headers={**AUTH, "Host": host, "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        return r.status_code


def test_allowed_host_vs_refused() -> None:
    # Refused by default (DNS-rebinding protection) ...
    assert _mcp_status("router.lan:8400", ()) == 421
    # ... accepted once the operator allows it (authenticated, so 421 is the guard).
    assert _mcp_status("router.lan:8400", ("router.lan",)) != 421
    assert _mcp_status("other.lan:8400", ("router.lan",)) == 421
    # behind a reverse proxy on 443 the Host header carries no port
    assert _mcp_status("router.lan", ("router.lan",)) != 421
    assert _mcp_status("other.lan", ("router.lan",)) == 421


def test_portless_origin_accepted_unlisted_refused() -> None:
    def status(origin: str) -> int:
        with edge_client(allowed_hosts=("router.lan",)) as (_app, c):
            r = c.post(
                "/mcp",
                headers={
                    **AUTH,
                    "Host": "router.lan",
                    "Origin": origin,
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            )
            return r.status_code

    assert status("https://router.lan") not in (403, 421)
    assert status("https://evil.lan") in (403, 421)


def test_transport_security_shape() -> None:
    ts = gateway_transport_security(("router.lan", "10.0.0.5:8400"))
    assert ts.enable_dns_rebinding_protection
    assert "router.lan:*" in ts.allowed_hosts and "10.0.0.5:8400" in ts.allowed_hosts
    assert "localhost:*" in ts.allowed_hosts
    assert "https://router.lan:*" in ts.allowed_origins
    assert "router.lan" in ts.allowed_hosts
    assert {"http://router.lan", "https://router.lan"} <= set(ts.allowed_origins)
    assert "10.0.0.5" not in ts.allowed_hosts
