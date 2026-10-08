"""Settings env plumbing added at integration (empty string means unset)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcprouter.settings import Settings


def test_new_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_DECISION_TIMEOUT_S", "0.5")
    monkeypatch.setenv("MCPR_IDLE_UNLOAD_S", "60")
    monkeypatch.setenv("MCPR_EMBED_BATCH_SIZE", "8")
    monkeypatch.setenv("MCPR_LAYA_NOUL_MODE", "native")
    s = Settings.from_env()
    assert (s.decision_timeout_s, s.idle_unload_s, s.embed_batch_size, s.laya_noul_mode) == (
        0.5,
        60.0,
        8,
        "native",
    )


def test_empty_string_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MCPR_DECISION_TIMEOUT_S",
        "MCPR_IDLE_UNLOAD_S",
        "MCPR_EMBED_BATCH_SIZE",
        "MCPR_LAYA_NOUL_MODE",
    ):
        monkeypatch.setenv(name, "  ")
    s, d = Settings.from_env(), Settings()
    assert (s.decision_timeout_s, s.idle_unload_s, s.embed_batch_size, s.laya_noul_mode) == (
        d.decision_timeout_s,
        d.idle_unload_s,
        d.embed_batch_size,
        d.laya_noul_mode,
    )


# wave-3 remote decision backend
def test_remote_decision_defaults() -> None:
    s = Settings()
    assert s.decision_endpoint == "https://api.aimlapi.com/v1/decisions"
    assert s.decision_model == "typesafe/jev"
    assert s.decision_api_key == ""
    assert s.decision_max_retries == 2


def test_remote_decision_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_DECISION_BACKEND", "remote")
    monkeypatch.setenv("MCPR_DECISION_ENDPOINT", "https://edge.example/v2/d")
    monkeypatch.setenv("MCPR_DECISION_MODEL", "typesafe/jev-x")
    monkeypatch.setenv("MCPR_DECISION_API_KEY", "envkey")
    monkeypatch.setenv("MCPR_DECISION_MAX_RETRIES", "0")
    monkeypatch.delenv("MCPR_DECISION_API_KEY_FILE", raising=False)
    s = Settings.from_env()
    assert s.decision_backend == "remote"
    assert s.decision_endpoint == "https://edge.example/v2/d"
    assert s.decision_model == "typesafe/jev-x"
    assert s.decision_api_key == "envkey"
    assert s.decision_max_retries == 0
    assert "envkey" not in repr(s)


def test_remote_decision_empty_means_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MCPR_DECISION_ENDPOINT",
        "MCPR_DECISION_MODEL",
        "MCPR_DECISION_API_KEY",
        "MCPR_DECISION_API_KEY_FILE",
        "MCPR_DECISION_MAX_RETRIES",
    ):
        monkeypatch.setenv(name, "")
    s, d = Settings.from_env(), Settings()
    assert (s.decision_endpoint, s.decision_model, s.decision_api_key, s.decision_max_retries) == (
        d.decision_endpoint,
        d.decision_model,
        d.decision_api_key,
        d.decision_max_retries,
    )


def test_remote_decision_key_file_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    f = tmp_path / "key"
    f.write_text("filekey\n")
    monkeypatch.setenv("MCPR_DECISION_API_KEY", "envkey")
    monkeypatch.setenv("MCPR_DECISION_API_KEY_FILE", str(f))
    assert Settings.from_env().decision_api_key == "filekey"


def test_remote_decision_key_file_unreadable_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_DECISION_API_KEY_FILE", "/nonexistent/mcpr-key")
    with pytest.raises(ValueError, match="MCPR_DECISION_API_KEY_FILE"):
        Settings.from_env()


def test_remote_decision_negative_retries_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCPR_DECISION_MAX_RETRIES", "-1")
    with pytest.raises(ValueError, match="MCPR_DECISION_MAX_RETRIES"):
        Settings.from_env()


def test_decision_key_file_stripped_and_capped(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from mcprouter.settings import Settings

    f = tmp_path / "key"
    f.write_text("  sk-abc \n")
    monkeypatch.setenv("MCPR_DECISION_API_KEY_FILE", str(f))
    assert Settings.from_env().decision_api_key == "sk-abc"
    f.write_text("x" * (64 * 1024 + 1))
    with pytest.raises(ValueError, match="MCPR_DECISION_API_KEY_FILE"):
        Settings.from_env()


@pytest.mark.parametrize("raw", ["-1", "11", "1000"])
def test_decision_max_retries_bounded(raw, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from mcprouter.settings import Settings

    monkeypatch.setenv("MCPR_DECISION_MAX_RETRIES", raw)
    with pytest.raises(ValueError, match="MCPR_DECISION_MAX_RETRIES"):
        Settings.from_env()


@pytest.mark.parametrize("bad", ["kéy", "k ey", "k\x01ey"])
def test_decision_key_must_be_printable_ascii(bad: str, monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MCPR_DECISION_API_KEY", bad)
    with pytest.raises(ValueError) as ei:
        Settings.from_env()
    assert bad not in str(ei.value)
    monkeypatch.delenv("MCPR_DECISION_API_KEY")
    f = tmp_path / "key"
    f.write_text(bad + "\n", encoding="utf-8")
    monkeypatch.setenv("MCPR_DECISION_API_KEY_FILE", str(f))
    with pytest.raises(ValueError) as ei:
        Settings.from_env()
    assert bad not in str(ei.value)
