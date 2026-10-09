"""Settings/hardening fixtures (wave-6 E1). Registered via pytest_plugins.

D2: the app-wide HostGuard admits only loopback + MCPR_ALLOWED_HOSTS. The
Starlette TestClient sends ``Host: testserver``, so every test admits that one
name through the module seam ``hardening._EXTRA_HOSTS`` (never through env).
Tests that prove the guard itself use ``Host: evil.example``.

P-109: DNS resolution in net_policy is replaced by an offline resolver.
"""

from __future__ import annotations

import socket

import pytest

from mcprouter import net_policy
from mcprouter.api import hardening


@pytest.fixture(autouse=True)
def _admit_testserver_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardening, "_EXTRA_HOSTS", frozenset({"testserver"}))


def _offline_resolver(host: str, port: object, **_: object) -> list[tuple[object, ...]]:
    raise socket.gaierror(socket.EAI_NONAME, "offline test resolver")


@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """P-109: net_policy resolves host names before connecting. The suite never
    does real DNS; unresolvable names pass (nothing to connect to), and
    tests/test_net_policy.py installs its own fake resolver."""
    monkeypatch.setattr(net_policy, "_RESOLVER", _offline_resolver)
