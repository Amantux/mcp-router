"""Settings env plumbing added at integration (empty string means unset)."""

from __future__ import annotations

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


# wave-3 Azure OpenAI backends
def test_aoai_settings_defaults_and_empty_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from mcprouter.settings import AoaiSettings

    for n in ("ENDPOINT", "API_KEY", "API_KEY_FILE", "CHAT_DEPLOYMENT", "EMBEDDING_DEPLOYMENT"):
        monkeypatch.setenv(f"MCPR_AOAI_{n}", "  ")
    monkeypatch.setenv("MCPR_AOAI_MAX_RETRIES", "")
    s = AoaiSettings.from_env()
    assert s == AoaiSettings()
    assert s.max_retries == 2


def test_aoai_key_file_wins_and_is_stripped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    from pathlib import Path

    from mcprouter.settings import AoaiSettings

    f = Path(str(tmp_path)) / "key"
    f.write_text("file-key\n", encoding="utf-8")
    monkeypatch.setenv("MCPR_AOAI_API_KEY", "env-key")
    monkeypatch.setenv("MCPR_AOAI_API_KEY_FILE", str(f))
    monkeypatch.setenv("MCPR_AOAI_ENDPOINT", "https://r.openai.azure.com")
    monkeypatch.setenv("MCPR_AOAI_CHAT_DEPLOYMENT", "gpt-4o-mini")
    monkeypatch.setenv("MCPR_AOAI_EMBEDDING_DEPLOYMENT", "te3s")
    monkeypatch.setenv("MCPR_AOAI_MAX_RETRIES", "0")
    s = AoaiSettings.from_env()
    assert s.api_key == "file-key"
    assert (s.endpoint, s.chat_deployment, s.embedding_deployment, s.max_retries) == (
        "https://r.openai.azure.com",
        "gpt-4o-mini",
        "te3s",
        0,
    )
    assert "file-key" not in repr(s)
    monkeypatch.delenv("MCPR_AOAI_API_KEY_FILE")
    assert AoaiSettings.from_env().api_key == "env-key"


def test_aoai_settings_reject_bad_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory
) -> None:
    from pathlib import Path

    from mcprouter.settings import AoaiSettings

    monkeypatch.setenv("MCPR_AOAI_MAX_RETRIES", "99")
    with pytest.raises(ValueError):
        AoaiSettings.from_env()
    monkeypatch.delenv("MCPR_AOAI_MAX_RETRIES")
    empty = Path(str(tmp_path)) / "empty"
    empty.write_text("\n", encoding="utf-8")
    monkeypatch.setenv("MCPR_AOAI_API_KEY_FILE", str(empty))
    with pytest.raises(ValueError):
        AoaiSettings.from_env()
