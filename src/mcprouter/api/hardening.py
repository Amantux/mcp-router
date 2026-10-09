"""App-wide HTTP hardening (wave-6 W0-3 seam; E1 fills it).

``install`` is called LAST in ``create_app`` so whatever it adds is the
outermost middleware (D2: the ``HostGuard`` must see every request first).
Currently a no-op: behaviour-neutral."""

from __future__ import annotations

from fastapi import FastAPI

from mcprouter.settings import Settings


def install(app: FastAPI, settings: Settings) -> None:
    """Reserved outermost-middleware slot (E1: HostGuard, security headers,
    metrics gate). No-op for now."""
    del app, settings
