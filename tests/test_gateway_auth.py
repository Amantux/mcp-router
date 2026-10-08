"""Agent + admin authentication (FR-07): hashed keys, constant-time compare,
401 on any presented-but-invalid credential, dev mode only when nothing is
configured."""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable, Iterator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from mcprouter.api import deps_auth
from mcprouter.api.deps_auth import (
    AgentKeysConfigError,
    SecurityConfig,
    bootstrap_principals,
    get_principal,
    hash_key,
    parse_agent_keys,
    require_admin,
)
from mcprouter.models import AgentPrincipal
from mcprouter.settings import Settings

from .conftest import TEST_DB_URL, requires_db

pytestmark = requires_db

KEY_A = "ka_" + "x" * 40
KEY_B = "kb_" + "y" * 40
ADMIN = "adm_" + "z" * 40


def _app(
    factory: sessionmaker[Session], *, agent_keys: str = "", admin_token: str | None = None
) -> FastAPI:
    settings = Settings(database_url=TEST_DB_URL, agent_keys=agent_keys)
    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = factory
    app.state.security = SecurityConfig.build(settings, {"MCPR_ADMIN_TOKEN": admin_token or ""})
    with factory() as s:
        bootstrap_principals(s, settings, app.state.security)
        s.commit()

    @app.get("/whoami")
    def whoami(p: AgentPrincipal = Depends(get_principal)) -> dict[str, str]:
        return {"agent": p.agent_id}

    @app.get("/admin")
    def admin(_: None = Depends(require_admin)) -> dict[str, str]:
        return {"ok": "yes"}

    return app


@pytest.fixture()
def make_client(db: sessionmaker[Session]) -> Iterator[Callable[..., TestClient]]:
    deps_auth._reset_dev_warning_for_tests()

    def make(**kw: str | None) -> TestClient:
        return TestClient(_app(db, **kw))  # type: ignore[arg-type]

    yield make


# ------------------------------------------------------------------ parsing
def test_parse_agent_keys() -> None:
    assert parse_agent_keys("a:k1, b:k2") == [("a", "k1"), ("b", "k2")]
    assert parse_agent_keys("") == []


@pytest.mark.parametrize("spec", ["nokey", "a:", ":k", "a:k1,a:k2", "a b:k", "a:k k"])
def test_parse_agent_keys_rejects_malformed_without_echoing_keys(spec: str) -> None:
    with pytest.raises(AgentKeysConfigError) as ei:
        parse_agent_keys(spec)
    for secret in ("k1", "k2", "k k"):
        if secret in spec:
            assert secret not in str(ei.value)


# ---------------------------------------------------------------- bootstrap
def test_bootstrap_stores_hash_only_and_is_idempotent(
    db: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    settings = Settings(database_url=TEST_DB_URL, agent_keys=f"alice:{KEY_A}")
    cfg = SecurityConfig.build(settings, {})
    with db() as s:
        bootstrap_principals(s, settings, cfg)
        bootstrap_principals(s, settings, cfg)
        s.commit()
        rows = s.scalars(select(AgentPrincipal)).all()
    assert len(rows) == 1
    assert rows[0].key_hash == hashlib.sha256(KEY_A.encode()).hexdigest()
    assert KEY_A not in caplog.text


def test_bootstrap_rotates_changed_key(db: sessionmaker[Session]) -> None:
    for key in (KEY_A, KEY_B):
        settings = Settings(database_url=TEST_DB_URL, agent_keys=f"alice:{key}")
        with db() as s:
            bootstrap_principals(s, settings, SecurityConfig.build(settings, {}))
            s.commit()
    with db() as s:
        row = s.scalars(select(AgentPrincipal)).one()
    assert row.key_hash == hash_key(KEY_B)


def test_bootstrap_refuses_agent_key_equal_to_admin_token(db: sessionmaker[Session]) -> None:
    settings = Settings(database_url=TEST_DB_URL, agent_keys=f"alice:{ADMIN}")
    cfg = SecurityConfig.build(settings, {"MCPR_ADMIN_TOKEN": ADMIN})
    with db() as s, pytest.raises(AgentKeysConfigError):
        bootstrap_principals(s, settings, cfg)


# ------------------------------------------------------------- agent auth
def test_valid_key_resolves_its_own_principal(make_client: Callable[..., TestClient]) -> None:
    c = make_client(agent_keys=f"alice:{KEY_A},bob:{KEY_B}")
    assert c.get("/whoami", headers={"Authorization": f"Bearer {KEY_A}"}).json() == {
        "agent": "alice"
    }
    assert c.get("/whoami", headers={"Authorization": f"bearer {KEY_B}"}).json() == {"agent": "bob"}


@pytest.mark.parametrize(
    "header",
    [
        None,
        f"Bearer {KEY_A}x",
        "Bearer ",
        "Bearer",
        f"Basic {KEY_A}",
        f"Bearer  {KEY_A}",
        f"Bearer {KEY_A} extra",
        f"{KEY_A}",
        "Bearer " + "a" * 5000,
    ],
)
def test_invalid_or_missing_key_is_401(
    make_client: Callable[..., TestClient], header: str | None
) -> None:
    c = make_client(agent_keys=f"alice:{KEY_A}")
    headers = {} if header is None else {"Authorization": header}
    r = c.get("/whoami", headers=headers)
    assert r.status_code == 401
    assert r.headers.get("www-authenticate") == "Bearer"
    assert KEY_A not in r.text


def test_spoofed_identity_headers_are_ignored(make_client: Callable[..., TestClient]) -> None:
    c = make_client(agent_keys=f"alice:{KEY_A}")
    r = c.get("/whoami", headers={"X-Agent-Id": "alice", "X-Forwarded-User": "alice"})
    assert r.status_code == 401


def test_disabled_principal_cannot_authenticate(
    make_client: Callable[..., TestClient], db: sessionmaker[Session]
) -> None:
    c = make_client(agent_keys=f"alice:{KEY_A}")
    with db() as s:
        s.scalars(select(AgentPrincipal)).one().enabled = False
        s.commit()
    assert c.get("/whoami", headers={"Authorization": f"Bearer {KEY_A}"}).status_code == 401


def test_compare_runs_over_every_principal_without_early_exit(
    make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    c = make_client(agent_keys=f"alice:{KEY_A},bob:{KEY_B},carol:k_{'c' * 40}")
    calls: list[int] = []
    real = hmac.compare_digest

    def spy(a: str, b: str) -> bool:
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(deps_auth.hmac, "compare_digest", spy)
    for key in (KEY_A, KEY_B):
        calls.clear()
        assert c.get("/whoami", headers={"Authorization": f"Bearer {key}"}).status_code == 200
        assert len(calls) == 3


# ---------------------------------------------------------------- dev mode
def test_dev_mode_only_when_nothing_configured(
    make_client: Callable[..., TestClient], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    c = make_client()
    assert c.get("/whoami").json() == {"agent": deps_auth.DEV_AGENT_ID}
    c.get("/whoami")
    warnings = [r for r in caplog.records if "auth.dev_mode" in r.getMessage()]
    assert len(warnings) == 1


def test_dev_mode_presented_key_is_still_401(make_client: Callable[..., TestClient]) -> None:
    """Never a silent downgrade: a presented-but-unknown key is rejected even in dev mode."""
    c = make_client()
    assert c.get("/whoami", headers={"Authorization": f"Bearer {KEY_A}"}).status_code == 401


def test_admin_token_configured_disables_dev_mode(make_client: Callable[..., TestClient]) -> None:
    c = make_client(admin_token=ADMIN)
    assert c.get("/whoami").status_code == 401


def test_principal_in_db_disables_dev_mode(
    make_client: Callable[..., TestClient], db: sessionmaker[Session]
) -> None:
    c = make_client()
    with db() as s:
        s.add(AgentPrincipal(agent_id="api-made", key_hash=hash_key(KEY_A)))
        s.commit()
    assert c.get("/whoami").status_code == 401


# ------------------------------------------------------------------- admin
def test_admin_token_required_and_checked(make_client: Callable[..., TestClient]) -> None:
    c = make_client(agent_keys=f"alice:{KEY_A}", admin_token=ADMIN)
    assert c.get("/admin", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 200
    assert c.get("/admin", headers={"Authorization": f"Bearer {ADMIN}x"}).status_code == 401
    assert c.get("/admin").status_code == 401


def test_agent_key_is_not_an_admin_credential(make_client: Callable[..., TestClient]) -> None:
    c = make_client(agent_keys=f"alice:{KEY_A}", admin_token=ADMIN)
    assert c.get("/admin", headers={"Authorization": f"Bearer {KEY_A}"}).status_code == 401


def test_admin_fails_closed_when_unconfigured_outside_dev_mode(
    make_client: Callable[..., TestClient],
) -> None:
    c = make_client(agent_keys=f"alice:{KEY_A}")
    assert c.get("/admin", headers={"Authorization": f"Bearer {KEY_A}"}).status_code == 403
    assert c.get("/admin").status_code == 403


def test_admin_permissive_in_dev_mode(make_client: Callable[..., TestClient]) -> None:
    assert make_client().get("/admin").status_code == 200


def test_unconfigured_app_fails_closed(db: sessionmaker[Session]) -> None:
    app = FastAPI()
    app.state.session_factory = db

    @app.get("/whoami")
    def whoami(p: AgentPrincipal = Depends(get_principal)) -> dict[str, str]:
        return {"agent": p.agent_id}

    r = TestClient(app).get("/whoami")
    assert r.status_code == 503
