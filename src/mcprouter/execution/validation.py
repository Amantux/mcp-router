"""JSON-Schema argument validation with curated, value-free errors (FR-07).

Errors name the failing location and keyword only (e.g. `$.limit: type`,
`$: additionalProperties`). jsonschema's own messages embed the offending
VALUES ("'hunter2' is not of type 'integer'") and unexpected property names,
which may be secrets — they never leave this module.

Remote `$ref` is never fetched. VERIFIED, not assumed: jsonschema 4.26's
DEFAULT registry still retrieves remote refs with urlopen (deprecated
behaviour, a live SSRF from an upstream-controlled tool schema). We pass an
explicit empty `referencing.Registry()` with no retriever, so any non-local
ref is unresolvable and the call is refused
(`test_remote_ref_is_never_fetched` runs a live listener and asserts zero hits).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import exceptions as js_exc
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import Unresolvable

MAX_ERRORS = 10
_NO_RETRIEVAL: Registry[bool | Mapping[str, Any]] = Registry()


class ArgumentValidationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _path(err: js_exc.ValidationError) -> str:
    parts = ["$"]
    for p in err.absolute_path:
        parts.append(f"[{p}]" if isinstance(p, int) else f".{p}")
    return "".join(parts)


def validate_arguments(schema: dict[str, Any] | None, arguments: Any) -> dict[str, Any]:
    """Return the arguments (as a dict) or raise ArgumentValidationError."""
    if not isinstance(arguments, dict):
        raise ArgumentValidationError(["$: arguments must be an object"])
    if not isinstance(schema, dict) or not schema:
        # No declared schema: accept an object (MCP tools may take no args).
        return arguments
    try:
        cls = validator_for(schema)
        cls.check_schema(schema)
        validator = cls(schema, registry=_NO_RETRIEVAL)
        errors = sorted(
            validator.iter_errors(arguments), key=lambda e: [str(p) for p in e.absolute_path]
        )
    except js_exc.SchemaError:
        raise ArgumentValidationError(["tool input schema is invalid"]) from None
    except Unresolvable:
        raise ArgumentValidationError(["tool input schema has an unresolvable $ref"]) from None
    if errors:
        curated = [f"{_path(e)}: {e.validator}" for e in errors[:MAX_ERRORS]]
        raise ArgumentValidationError(curated)
    return arguments
