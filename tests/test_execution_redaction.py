"""Secret redaction + log scrubbing (FR-07). Fixtures are real-SHAPED, fake-VALUED."""

from __future__ import annotations

import time

import pytest

from mcprouter.execution.redaction import MASK, redact, redact_value, scrub_log

# Real-shaped fake secrets. None of these are live credentials.
FAKE_GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
FAKE_OPENAI = "sk-" + "proj-Zx8Yw7Vu6Ts5Rq4Po3Nm2Lk1Ji0Hg9Fe8Dc"
FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"
FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkZha2UifQ"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)
FAKE_SLACK = "xoxb-" + "1234567890-0987654321-AbCdEfGhIjKlMnOpQrStUv"
FAKE_ENTROPY = "q7Zr2LmX9vK4pW8sT3nB6yH1cJ5fD0gA"  # 32 chars, mixed classes


@pytest.mark.parametrize(
    "secret",
    [FAKE_GH, FAKE_OPENAI, FAKE_AWS, FAKE_JWT, FAKE_SLACK, FAKE_ENTROPY],
)
def test_known_token_shapes_are_masked(secret: str) -> None:
    out = redact(f"calling upstream with {secret} now")
    assert secret not in out
    assert MASK in out
    assert out.startswith("calling upstream with ")


def test_bearer_header_masked() -> None:
    out = redact("Authorization: Bearer abc.def-ghi_123")
    assert "abc.def-ghi_123" not in out
    assert "Bearer" in out or MASK in out


@pytest.mark.parametrize(
    "text,leak",
    [
        ("password=hunter2", "hunter2"),
        ("PASSWORD = 'hunter2'", "hunter2"),
        ('{"api_key": "plainvalue"}', "plainvalue"),
        ("db_password: s3cr3t", "s3cr3t"),
        ("client_secret=abc&other=1", "abc"),
        ("github_token=shortone", "shortone"),
        ("secret:topsy", "topsy"),
        ("x-api-key=zzz", "zzz"),
    ],
)
def test_key_value_secrets_masked(text: str, leak: str) -> None:
    out = redact(text)
    assert leak not in out, out


def test_key_value_keeps_benign_neighbours() -> None:
    out = redact("client_secret=abc&other=1")
    assert "other=1" in out


@pytest.mark.parametrize(
    "url,leak",
    [
        ("postgresql://admin:pa55word@db.internal:5432/app", "pa55word"),
        ("https://user:tok@example.com/x", "tok"),
        ("redis://:onlypass@cache:6379/0", "onlypass"),
    ],
)
def test_url_userinfo_masked(url: str, leak: str) -> None:
    out = redact(f"connect to {url} failed")
    assert leak not in out
    assert "db.internal" in out or "example.com" in out or "cache" in out


def test_benign_text_untouched() -> None:
    text = "Listed 3 issues in repo acme/widgets; see https://example.com/issues?page=2"
    assert redact(text) == text


def test_uuid_ids_are_not_redacted() -> None:
    # Audit detail carries tool/server UUIDs; they must stay readable.
    text = "tool 3f1c2b9e-8d7a-4c6b-9e5f-1a2b3c4d5e6f denied"
    assert redact(text) == text


def test_long_plain_word_not_redacted() -> None:
    text = "supercalifragilisticexpialidocious_is_a_long_word"
    assert redact(text) == text


def test_redact_value_masks_secret_named_keys_recursively() -> None:
    value = {
        "path": "/tmp/report.txt",
        "token": "anything-at-all",
        "nested": {"Password": 12345, "items": [{"api_key": "k"}, "fine"]},
        "note": f"use {FAKE_GH}",
    }
    out = redact_value(value)
    assert out["path"] == "/tmp/report.txt"
    assert out["token"] == MASK
    assert out["nested"]["Password"] == MASK
    assert out["nested"]["items"][0]["api_key"] == MASK
    assert out["nested"]["items"][1] == "fine"
    assert FAKE_GH not in out["note"]


def test_redact_value_does_not_mutate_input() -> None:
    value = {"token": "x"}
    redact_value(value)
    assert value == {"token": "x"}


def test_scrub_log_neutralizes_crlf_and_controls() -> None:
    out = scrub_log("agent\r\nFAKE LOG LINE level=critical\x00\u2028x")
    assert "\r" not in out and "\n" not in out and "\x00" not in out and "\u2028" not in out
    assert "\\r\\n" in out


def test_scrub_log_also_redacts() -> None:
    assert FAKE_GH not in scrub_log(f"key {FAKE_GH}\n")


@pytest.mark.parametrize(
    "evil",
    [
        "a" * 200_000,  # one giant token-class run
        "password=" * 50_000,  # repeated key prefixes
        "x://" + "a" * 100_000 + "@",  # userinfo-ish run
        "Bearer " * 50_000,
        ("ab:" * 70_000),
        "-" * 200_000 + "!",
    ],
    ids=["run", "kv-prefix", "userinfo", "bearer", "colons", "dashes"],
)
def test_no_catastrophic_backtracking(evil: str) -> None:
    start = time.perf_counter()
    redact(evil)
    assert time.perf_counter() - start < 1.5
