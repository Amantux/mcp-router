"""P-106: secret settings never leak (repr, logs, error text), every secret
has a `_FILE` variant that wins, and env-provided keys meet a minimum length."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from mcprouter.settings import SETTINGS_SPEC, AoaiSettings, Settings

LONG = "x" * 32
CANARIES = {
    "MCPR_DATABASE_URL": "postgresql+psycopg://u:canary-dsn-pw-0001@db/x",
    "MCPR_AGENT_KEYS": "bot:canary-agent-key-000000000000000002",
    "MCPR_ADMIN_TOKEN": "canary-admin-token-00000000000000000003",
    "MCPR_DECISION_API_KEY": "canary-decision-key-0004",
    "MCPR_AOAI_API_KEY": "canary-aoai-key-0005",
}
# The distinctive part of each canary (what must never appear anywhere).
NEEDLES = (
    "canary-dsn-pw-0001",
    "canary-agent-key",
    "canary-admin-token",
    "canary-decision",
    "canary-aoai",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in list(os.environ):
        if k.startswith("MCPR_"):
            monkeypatch.delenv(k)


def _set_all(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in CANARIES.items():
        monkeypatch.setenv(k, v)


def test_secret_vars_are_exactly_the_canaries() -> None:
    assert {s.env for s in SETTINGS_SPEC if s.secret} == set(CANARIES)
    assert all(s.file_var == f"{s.env}_FILE" for s in SETTINGS_SPEC if s.secret)


def test_canaries_absent_from_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all(monkeypatch)
    s = Settings.from_env()
    assert s.admin_token == CANARIES["MCPR_ADMIN_TOKEN"]  # parsed, just hidden
    for text in (repr(s), str(s), repr(s.aoai()), repr(AoaiSettings.from_env())):
        for needle in NEEDLES:
            assert needle not in text, needle


def test_canaries_absent_from_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from mcprouter.logging import configure_logging

    _set_all(monkeypatch)
    s = Settings.from_env()
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        configure_logging(s)
        with caplog.at_level(logging.DEBUG):
            logging.getLogger("mcprouter.probe").info("settings=%r", s)
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
    for needle in NEEDLES:
        assert needle not in caplog.text


@pytest.mark.parametrize("var", sorted(CANARIES))
def test_file_variant_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, var: str) -> None:
    other = CANARIES[var].replace("canary", "fromfile")
    f = tmp_path / "secret"
    f.write_text(f"  {other}\n")
    monkeypatch.setenv(var, CANARIES[var])
    monkeypatch.setenv(f"{var}_FILE", str(f))
    spec = next(s for s in SETTINGS_SPEC if s.env == var)
    assert getattr(Settings.from_env(), spec.name) == other


@pytest.mark.parametrize("var", sorted(CANARIES))
@pytest.mark.parametrize("problem", ["missing", "oversized", "not-utf8", "empty"])
def test_bad_file_names_var_not_content_or_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, var: str, problem: str
) -> None:
    f = tmp_path / "canary-path-secret"
    if problem == "oversized":
        f.write_text("canary-content" * 6000)
    elif problem == "not-utf8":
        f.write_bytes(b"canary-content\xff\xfe")
    elif problem == "empty":
        f.write_text(" \n")
    monkeypatch.setenv(f"{var}_FILE", str(f))
    with pytest.raises(ValueError) as ei:
        Settings.from_env()
    msg = str(ei.value)
    assert msg.startswith(f"{var}_FILE:")
    assert "canary" not in msg and str(tmp_path) not in msg


def test_aoai_unreadable_key_file_is_curated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_AOAI_API_KEY_FILE", "/nonexistent/canary-aoai-path")
    with pytest.raises(ValueError) as ei:
        AoaiSettings.from_env()
    assert "canary" not in str(ei.value) and "MCPR_AOAI_API_KEY_FILE" in str(ei.value)


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("MCPR_ADMIN_TOKEN", "short-canary-token"),
        ("MCPR_AGENT_KEYS", "bot:short-canary"),
        ("MCPR_AGENT_KEYS", f"a:{LONG}, b:short-canary"),
    ],
)
def test_short_keys_rejected_without_echo(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(ValueError) as ei:
        Settings.from_env()
    assert str(ei.value).startswith(f"{var}:") and "canary" not in str(ei.value)


def test_long_keys_accepted_and_list_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_ADMIN_TOKEN", LONG)
    monkeypatch.setenv("MCPR_AGENT_KEYS", f" a:{LONG} , b:{LONG}y ,")
    s = Settings.from_env()
    assert s.admin_token == LONG and s.agent_keys == f"a:{LONG},b:{LONG}y"


def test_kwargs_path_unchanged() -> None:
    # Tests and embedders build Settings directly: no length policy there.
    assert Settings(admin_token="x", agent_keys="a:k").admin_token == "x"


def test_secret_with_control_chars_rejected_without_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_AGENT_KEYS", f"a:canary{LONG}\x07")
    with pytest.raises(ValueError) as ei:
        Settings.from_env()
    assert "canary" not in str(ei.value)


def test_admin_token_file_reaches_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from mcprouter.api.app import create_app
    from tests.conftest import TEST_DB_URL, _db_available

    if not _db_available():
        pytest.skip("compose db not running")
    f = tmp_path / "admin"
    f.write_text(CANARIES["MCPR_ADMIN_TOKEN"] + "\n")
    monkeypatch.setenv("MCPR_ADMIN_TOKEN_FILE", str(f))
    monkeypatch.setenv("MCPR_DATABASE_URL", TEST_DB_URL)
    with TestClient(create_app(Settings.from_env(), env={})) as c:
        ok = c.get(
            "/api/v1/servers",
            headers={"Authorization": f"Bearer {CANARIES['MCPR_ADMIN_TOKEN']}"},
        )
        assert ok.status_code == 200
        assert c.get("/api/v1/servers", headers={"Authorization": "Bearer wrong"}).status_code in (
            401,
            403,
        )
