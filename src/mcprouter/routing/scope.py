"""Concrete `ScopeFilter` implementations owned by the routing track.

The REAL per-agent scope (PolicyRule-backed, deny-by-default) is the gateway
track's: it implements `interfaces.ScopeFilter` and injects it. These two are
the only ones routing ships:

* `AllowAllScope` — the permissive DEV default for the route endpoint when no
  gateway resolver is installed. It is NOT an authorization decision.
* `StaticScope` — a fixed server list + operation ceiling; used by the eval
  framework to express "this agent may only read" without depending on the
  gateway's policy engine.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcprouter.interfaces import ScopeFilter, ToolCandidate

_OP_RANK = {"read": 0, "write": 1, "execute": 2}


class AllowAllScope:
    def server_ids(self) -> list[str] | None:
        return None

    def fingerprint(self) -> str:  # route-cache key component
        return "allow-all"

    def permits(self, candidate: ToolCandidate) -> bool:
        return True

    def principal_max_skills(self) -> int | None:
        # No principal: no principal skills ceiling (the global cap applies).
        return None


@dataclass(frozen=True)
class StaticScope:
    servers: tuple[str, ...] | None = None
    max_operation: str = "execute"  # read < write < execute

    def server_ids(self) -> list[str] | None:
        return None if self.servers is None else list(self.servers)

    def fingerprint(self) -> str:  # route-cache key component
        servers = "*" if self.servers is None else ",".join(sorted(self.servers))
        return f"static:{self.max_operation}:{servers}"

    def principal_max_skills(self) -> int | None:
        return None  # eval scope: no principal ceiling; the global cap applies

    def permits(self, candidate: ToolCandidate) -> bool:
        # Deny by default: an unknown/unclassified operation is not provably
        # within any ceiling, so it is refused.
        op = _OP_RANK.get(candidate.operation)
        ceiling = _OP_RANK.get(self.max_operation)
        return op is not None and ceiling is not None and op <= ceiling


class UncachedScope:
    """Delegates to another scope but deliberately has NO `fingerprint()`, so
    the route cache never keys (and never serves) routes made under it. Used
    by /route/evaluate: an eval re-run must measure the pipeline, not the cache."""

    def __init__(self, inner: ScopeFilter) -> None:
        self._inner = inner

    def server_ids(self) -> list[str] | None:
        return self._inner.server_ids()

    def permits(self, candidate: ToolCandidate) -> bool:
        return self._inner.permits(candidate)

    def principal_max_skills(self) -> int | None:
        # Delegate, or the wrapped principal's skills budget is lost (S2d review A).
        return principal_skill_cap(self._inner)


def principal_skill_cap(scope: ScopeFilter) -> int | None:
    """The principal's skills ceiling for `scope`; None = no principal ceiling.

    Resolution: the scope's public `principal_max_skills()`, else FAIL
    CLOSED to 0. A scope that cannot say what its principal may see gets no
    skills, never the global cap."""
    public = getattr(scope, "principal_max_skills", None)
    if callable(public):
        value = public()
        return value if value is None or isinstance(value, int) else 0
    return 0
