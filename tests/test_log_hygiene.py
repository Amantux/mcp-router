"""P-610: one strong log sanitiser, and curated exception messages that can
only interpolate allow-listed values (A6-016, A6-032)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mcprouter.discovery import logsafe
from mcprouter.execution.redaction import scrub_log
from mcprouter.inference.laya import _scrub as laya_scrub
from mcprouter.registry.audit import scrub as audit_scrub

SRC = Path(__file__).resolve().parents[1] / "src" / "mcprouter"
HOSTILE = "\x1b[2J\r\nlevel=ERROR forged \x00\x7f\u0085 end"


@pytest.mark.parametrize(
    "helper", [scrub_log, audit_scrub, logsafe.scrub, laya_scrub], ids=lambda f: f.__module__
)
def test_every_log_helper_strips_control_characters(helper: object) -> None:
    assert callable(helper)
    out = helper(HOSTILE)
    assert not any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in out), repr(out)
    assert "forged" in out


def test_logsafe_redact_is_mask_secrets() -> None:
    assert logsafe.redact is logsafe.mask_secrets
    assert logsafe.mask_secrets("token=abcd1234 x", ["abcd1234"]) == "token=*** x"


# ------------------------------------------------- curated exception messages
# Exceptions whose str() is returned to API/MCP callers. Their messages may
# interpolate only values that are known-safe: identifiers we validated,
# numbers, enum-like names, class names. Never a URL, path, upstream body or
# a raw exception.
CURATED = {
    "AgentKeysConfigError",
    "AuthenticationError",
    "FeedbackError",
    "FeedbackInvalid",
    "FeedbackNotFound",
    "FeedbackRateLimited",
    "InvalidEndpointError",
    "InvalidWindow",
    "ModelUnavailableError",
    "RollupNotAllowed",
    "SchemaVersionError",
    "SkillValidationError",
    "SourceError",
    "StalenessTimeout",
}
# Expressions reviewed as safe in a curated message (source text of the
# interpolated expression). Adding one is a review decision.
ALLOWED_EXPRS = {
    "key",  # skills/validate.py: a frontmatter field name chosen by the code
    "max_len",  # skills/validate.py: an int limit
    "MAX_ITEMS",  # analytics/feedback.py: module constant
    "MAX_DAYS_PER_RUN",  # analytics/rollup.py: module constant
    "idx + 1",  # auth/keys.py: entry number in MCPR_AGENT_KEYS, never the entry
    "agent_id",  # auth/keys.py, principals.py: regex-validated [A-Za-z0-9_.-]{1,120}
    "type(exc).__name__",  # inference/engine.py: class name only, never str(exc)
}


def curated_interpolations(source: str, filename: str = "<src>") -> list[str]:
    """`file:line expr` for every interpolated expression in a raise of a
    curated class that is not in ALLOWED_EXPRS."""
    bad: list[str] = []
    for node in ast.walk(ast.parse(source, filename)):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        func = node.exc.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in CURATED:
            continue
        for arg in [*node.exc.args, *(k.value for k in node.exc.keywords)]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.FormattedValue):
                    expr = ast.unparse(sub.value)
                    if expr not in ALLOWED_EXPRS:
                        bad.append(f"{filename}:{node.lineno} {{{expr}}}")
                elif isinstance(sub, ast.Call) and getattr(sub.func, "attr", "") == "format":
                    bad.append(f"{filename}:{node.lineno} .format(...)")
    return bad


def test_curated_exceptions_interpolate_only_allowlisted_values() -> None:
    bad: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        bad += curated_interpolations(path.read_text(), path.relative_to(SRC).as_posix())
    assert bad == [], "curated exception message interpolates an unreviewed value: " + str(bad)


def test_the_checker_catches_a_url_in_a_curated_message() -> None:
    src = 'def f(url):\n    raise InvalidEndpointError(f"cannot reach {url}")\n'
    assert curated_interpolations(src) == ["<src>:2 {url}"]
    ok = 'def f(key):\n    raise SkillValidationError(f"{key} must be a string")\n'
    assert curated_interpolations(ok) == []
