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
