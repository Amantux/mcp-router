"""App-wide exception handlers (wave-6 W0-3 seam; E2 fills it).

No-op for now. The curated ``/route`` 422 handler stays where it is
(``routes_route.install_routing``) until E2 moves it here."""

from __future__ import annotations

from fastapi import FastAPI


def install_error_handlers(app: FastAPI) -> None:
    """Register app-wide exception handlers. No-op for now."""
    del app
