"""Process logging configuration (wave-6 E1, D4).

structlog renders stdlib records through ``structlog.stdlib.ProcessorFormatter``
on ONE root handler (stderr). ``MCPR_LOG_LEVEL`` (default INFO) sets the root
level, ``MCPR_LOG_FORMAT`` picks ``console`` (default) or ``json`` (compose).
uvicorn's loggers are routed through the same handler.

Every record passes two processors before rendering:
* redaction: configured secret VALUES (admin token, agent keys, decision/AOAI
  API keys, the database password) are replaced by ``[REDACTED]`` anywhere in
  the event, its fields or a rendered traceback;
* scrubbing: CR/LF and other control characters (incl. ANSI escapes) are
  escaped, so a hostile value cannot forge log lines or drive a terminal.
  Traceback lines keep their newline but every continuation is indented.

Named ``mcprouter.logging``; absolute imports mean it never shadows the stdlib
``logging`` module. Idempotent: calling it again replaces our handler.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, MutableMapping
from typing import Any, TextIO

import structlog

from mcprouter.settings import Settings

_HANDLER_MARK = "_mcpr_handler"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
_MIN_SECRET_LEN = 8  # shorter values would redact ordinary words
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
REDACTED = "[REDACTED]"


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Resolves sys.stderr at emit time (pytest/capsys swap it)."""

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        pass


def secret_values(settings: Settings) -> tuple[str, ...]:
    """Every configured secret value worth redacting, longest first."""
    found: set[str] = set()
    for v in (settings.admin_token, settings.decision_api_key):
        found.add(v)
    for entry in settings.agent_keys.split(","):
        found.add(entry.partition(":")[2].strip())
    try:
        from sqlalchemy.engine import make_url

        found.add(make_url(settings.database_url).password or "")
    except Exception:  # noqa: BLE001 - a malformed DSN fails elsewhere, curated
        pass
    return tuple(sorted((s for s in found if len(s) >= _MIN_SECRET_LEN), key=len, reverse=True))


def _escape_controls(text: str) -> str:
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


def scrub(text: str, secrets: Iterable[str] = (), *, multiline: bool = False) -> str:
    """Redact `secrets`, then neutralise CR/LF/control characters."""
    for s in secrets:
        text = text.replace(s, REDACTED)
    text = text.replace("\r", "\\r")
    if multiline:
        text = "\n    ".join(text.split("\n"))
    else:
        text = text.replace("\n", "\\n")
    return _escape_controls(text)


def _make_scrubber(secrets: tuple[str, ...]) -> structlog.types.Processor:
    def scrubber(_logger: Any, _name: str, event_dict: MutableMapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in event_dict.items():
            if key == "event" and not isinstance(value, str):
                value = str(value)
            if isinstance(value, str):
                value = scrub(value, secrets, multiline=key in ("exception", "stack"))
            out[key] = value
        return out

    return scrubber


def configure_logging(settings: Settings) -> None:
    """Install the one root handler for `settings.log_level`/`log_format`."""
    level = logging.getLevelNamesMapping()[settings.log_level.upper()]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    pre_chain: list[structlog.types.Processor] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            _make_scrubber(secret_values(settings)),
            renderer,
        ],
    )
    handler = _StderrHandler()
    setattr(handler, _HANDLER_MARK, True)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    for name in _UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True

    structlog.configure(
        processors=[*pre_chain, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
