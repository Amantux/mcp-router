"""Process logging configuration (wave-6 W0-3 seam; E1 fills it, D4).

Named ``mcprouter.logging``; absolute imports mean it never shadows the stdlib
``logging`` module. No-op for now: behaviour-neutral."""

from __future__ import annotations

from mcprouter.settings import Settings


def configure_logging(settings: Settings) -> None:
    """Configure handlers/format from ``settings.log_level``/``log_format``.
    No-op for now."""
    del settings
