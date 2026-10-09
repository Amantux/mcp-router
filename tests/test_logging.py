"""P-104 (D4): structlog-backed stdlib logging with redaction and scrubbing."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from mcprouter.logging import REDACTED, configure_logging, scrub, secret_values
from mcprouter.settings import Settings

CANARY = "canary-admin-token-0123456789abcdef"
DB_PW = "canary-db-password-xyz"


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "admin_token": CANARY,
        "database_url": f"postgresql+psycopg://u:{DB_PW}@db/x",
        "agent_keys": "bot:canary-agent-key-0123456789abcdef",
        "decision_api_key": "canary-decision-key-0123456789",
    }
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def test_info_emitted_with_level_and_logger(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(_settings(log_format="console"))
    logging.getLogger("mcprouter.probe").info("hello %s", "world")
    err = capsys.readouterr().err
    assert "hello world" in err and "info" in err and "mcprouter.probe" in err


def test_json_lines_parse(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(_settings(log_format="json"))
    logging.getLogger("mcprouter.probe").warning("a\nb")
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
    rec = json.loads(lines[-1])
    assert rec["level"] == "warning" and rec["logger"] == "mcprouter.probe"
    assert rec["event"] == "a\\nb"  # CR/LF escaped before rendering
    assert "timestamp" in rec


def test_level_respected(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(_settings(log_level="WARNING"))
    logging.getLogger("mcprouter.probe").info("quiet-info")
    assert "quiet-info" not in capsys.readouterr().err


@pytest.mark.parametrize("fmt", ["console", "json"])
def test_secrets_never_in_output(fmt: str, capsys: pytest.CaptureFixture[str]) -> None:
    s = _settings(log_format=fmt)
    configure_logging(s)
    log = logging.getLogger("mcprouter.probe")
    try:
        raise RuntimeError(f"boom token={CANARY} dsn-pw={DB_PW}")
    except RuntimeError:
        log.exception("failed with %s", CANARY)
    log.info("agent key canary-agent-key-0123456789abcdef and canary-decision-key-0123456789")
    err = capsys.readouterr().err
    for secret in secret_values(s):
        assert secret not in err
    assert CANARY not in err and DB_PW not in err
    assert REDACTED in err and "RuntimeError" in err


def test_ansi_and_crlf_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(_settings())
    logging.getLogger("mcprouter.probe").warning("x\x1b[2J\r\nFAKE level=critical")
    err = capsys.readouterr().err
    assert "\x1b" not in err and "\r" not in err
    assert not any(ln.startswith("FAKE") for ln in err.splitlines())


def test_uvicorn_loggers_routed_through_root(capsys: pytest.CaptureFixture[str]) -> None:
    uv = logging.getLogger("uvicorn.error")
    uv.addHandler(logging.StreamHandler())  # what uvicorn's own dictConfig installs
    configure_logging(_settings(log_format="json"))
    assert uv.handlers == [] and uv.propagate is True
    uv.warning("uvicorn says hi")
    rec = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert rec["logger"] == "uvicorn.error"


def test_idempotent_single_handler() -> None:
    configure_logging(_settings())
    configure_logging(_settings())
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_mcpr_handler", False)]
    assert len(ours) == 1


def test_scrub_multiline_keeps_traceback_indented() -> None:
    assert scrub("a\nb", multiline=True) == "a\n    b"
    assert scrub("tok-secret-value!", ["tok-secret-value"]) == f"{REDACTED}!"
