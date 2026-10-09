"""P-102 (D2): app-wide HostGuard + security headers on the REAL create_app.

TestClient's own "testserver" is admitted by the autouse fixture in
tests/support/settings.py; these tests send a foreign Host explicitly."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from mcprouter.api import hardening
from mcprouter.api.app import create_app
from mcprouter.settings import Settings
from tests.conftest import TEST_DB_URL, requires_db

pytestmark = requires_db

ADMIN = "hostguard-admin-token-0123456789abcdef"
EVIL = {"Host": "evil.example"}
PATHS = ("/api/v1/servers", "/metrics", "/docs", "/openapi.json", "/", "/healthz", "/mcp")


def _client(**kw: object) -> TestClient:
    s = Settings(database_url=TEST_DB_URL, **kw)  # type: ignore[arg-type]
    return TestClient(create_app(s, env={"MCPR_ADMIN_TOKEN": ADMIN}))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with _client() as c:
        yield c


@pytest.mark.parametrize("path", PATHS)
def test_foreign_host_is_421_everywhere(client: TestClient, path: str) -> None:
    r = client.get(path, headers=EVIL)
    assert r.status_code == 421, path
    assert r.json() == {
        "detail": "Misdirected request: Host not allowed (add it to MCPR_ALLOWED_HOSTS)"
    }


def test_foreign_host_refused_before_auth_and_body(client: TestClient) -> None:
    r = client.post(
        "/api/v1/servers",
        headers={**EVIL, "Authorization": f"Bearer {ADMIN}"},
        content=b"{not json",
    )
    assert r.status_code == 421
    # Outermost: refused before the body-size limit (which would say 413).
    big = client.post("/api/v1/servers", headers=EVIL, content=b"x" * (2 * 1024 * 1024))
    assert big.status_code == 421


def test_extra_hosts_seam_is_empty_in_production() -> None:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(hardening))
    seam = [
        n
        for n in tree.body
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "_EXTRA_HOSTS"
    ]
    assert len(seam) == 1 and seam[0].value is not None
    assert ast.unparse(seam[0].value) == "frozenset()"


def test_websocket_with_foreign_host_closed(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/mcp", headers=EVIL):
            pass


@pytest.mark.parametrize(
    "host", ["[::1]evil.com", "[::1]@evil.com", "localhost:evil.com", "localhost.", "::1"]
)
def test_malformed_host_refused(client: TestClient, host: str) -> None:
    assert client.get("/healthz", headers={"Host": host}).status_code == 421


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1:8400", "[::1]:9", "LOCALHOST:1"])
def test_loopback_hosts_admitted_port_insensitive(client: TestClient, host: str) -> None:
    assert client.get("/healthz", headers={"Host": host}).status_code == 200


def test_missing_host_header_refused() -> None:
    import anyio

    sent: list[dict[str, object]] = []

    async def app(scope: object, receive: object, send: object) -> None:  # pragma: no cover
        raise AssertionError("inner app must not run")

    async def send(msg: dict[str, object]) -> None:
        sent.append(msg)

    async def receive() -> dict[str, object]:  # pragma: no cover
        return {"type": "http.request"}

    guard = hardening.HostGuard(app, hardening.LOOPBACK_HOSTNAMES)
    scope = {"type": "http", "path": "/healthz", "headers": []}
    anyio.run(guard, scope, receive, send)  # type: ignore[arg-type]
    assert sent[0]["status"] == 421


def test_allowed_hosts_env_admits_name_any_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_ALLOWED_HOSTS", "router.lan")
    monkeypatch.setenv("MCPR_DATABASE_URL", TEST_DB_URL)
    s = Settings.from_env()
    with TestClient(create_app(s, env={"MCPR_ADMIN_TOKEN": ADMIN})) as c:
        assert c.get("/healthz", headers={"Host": "router.lan"}).status_code == 200
        assert c.get("/healthz", headers={"Host": "router.lan:8443"}).status_code == 200
        assert c.get("/healthz", headers={"Host": "other.lan"}).status_code == 421


def test_security_headers_on_json_200(client: TestClient) -> None:
    r = client.get("/api/v1/servers", headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_security_headers_on_healthz_and_spa(client: TestClient) -> None:
    h = client.get("/healthz")
    assert h.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in h.headers["content-security-policy"]
    spa = client.get("/")  # dist absent in tests -> the "not built" HTML page
    assert spa.headers["content-type"].startswith("text/html")
    csp = spa.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    docs = client.get("/docs")  # Swagger UI pulls a CDN: frame-ancestors only
    assert docs.headers["content-security-policy"] == "frame-ancestors 'none'"


@pytest.mark.parametrize(
    ("header", "name"),
    [("a.example:80", "a.example"), ("[::1]:8400", "[::1]"), ("[::1]", "[::1]"), ("X.Y", "x.y")],
)
def test_hostname_of(header: str, name: str) -> None:
    assert hardening.hostname_of(header) == name


# --- P-108 (D14): /metrics gate, curated /healthz, /readyz --------------------

H_ADMIN = {"Authorization": f"Bearer {ADMIN}"}


def test_metrics_requires_admin_when_token_set(client: TestClient) -> None:
    from mcprouter.analytics.metrics import install_metrics

    install_metrics(client.app.state.session_factory).refresh_now()  # type: ignore[attr-defined]
    assert client.get("/metrics/").status_code == 401
    assert client.get("/metrics/", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = client.get("/metrics/", headers=H_ADMIN)
    assert r.status_code == 200 and "mcpr_analytics_" in r.text


def test_metrics_open_without_admin_token() -> None:
    s = Settings(database_url=TEST_DB_URL)
    with TestClient(create_app(s, env={})) as c:
        assert c.get("/metrics/").status_code == 200


def test_healthz_db_down_is_curated_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from sqlalchemy.exc import OperationalError

    def boom() -> None:
        raise OperationalError("connect", {}, Exception("password=hunter2-dsn @db:5432"))

    monkeypatch.setattr(client.app.state.engine, "connect", boom)  # type: ignore[attr-defined]
    for path in ("/healthz", "/readyz"):
        r = client.get(path)
        assert r.status_code == 503
        assert r.json() == {"status": "unavailable", "detail": "database unreachable"}
        assert "hunter2" not in r.text
    assert "hunter2" not in caplog.text


def test_readyz_ready_and_minimal(client: TestClient) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ready", "db": "ok", "engineLoaded": True, "degraded": False}


def test_readyz_degraded_when_laya_requested_but_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "laya", None)  # import laya -> ImportError
    with _client(decision_backend="laya") as c:
        body = c.get("/readyz").json()
    assert body["status"] == "degraded" and body["degraded"] is True
