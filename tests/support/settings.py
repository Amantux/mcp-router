"""Settings/hardening fixtures (wave-6 E1). Registered via pytest_plugins.

D2: the app-wide HostGuard admits only loopback + MCPR_ALLOWED_HOSTS. The
Starlette TestClient sends ``Host: testserver``, so every test admits that one
name through the module seam ``hardening._EXTRA_HOSTS`` (never through env).
Tests that prove the guard itself use ``Host: evil.example``.
"""

from __future__ import annotations

import pytest

from mcprouter.api import hardening


@pytest.fixture(autouse=True)
def _admit_testserver_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardening, "_EXTRA_HOSTS", frozenset({"testserver"}))
