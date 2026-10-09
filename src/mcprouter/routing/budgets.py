"""Exposure budgets: the ONE clamp chain for "how many tools / servers may an
agent be shown" (REST /route, /route/simulate, the MCP gateway).

    effective = min(request value or principal default, principal cap, global cap)

A request may LOWER a budget, never raise it: every term is a ceiling and the
request only participates in the min(). `max_servers` is optional at every
level (None = unlimited); the effective value is the min of the levels that
are set, or None when none is.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import TypeVar

from mcprouter.models import AgentPrincipal
from mcprouter.settings import Settings

T = TypeVar("T")


@dataclass(frozen=True)
class BudgetClamp:
    """How one budget was resolved (surfaced by /route/simulate)."""

    budget: str  # "maxTools" | "maxServers"
    requested: int | None
    principal: int | None
    global_cap: int | None
    applied: int | None
    clamped_by: str | None  # "principal" | "global" | None (request value stood)


@dataclass(frozen=True)
class Budgets:
    max_tools: int
    max_servers: int | None
    clamps: tuple[BudgetClamp, ...]


def _min_set(*values: int | None) -> int | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


def _clamp(
    name: str, requested: int | None, principal: int | None, global_cap: int | None
) -> BudgetClamp:
    applied = _min_set(requested, principal, global_cap)
    # What the caller effectively asked for: its own value, else the
    # principal default. "Clamped" means a ceiling cut BELOW that.
    baseline = requested if requested is not None else principal
    clamped_by: str | None = None
    if applied is not None and baseline is not None and applied < baseline:
        # Attribute to the tightest ceiling; ties prefer the global cap (the
        # operator's setting) so the explanation is stable.
        if global_cap is not None and applied == global_cap:
            clamped_by = "global"
        elif principal is not None and applied == principal:
            clamped_by = "principal"
    return BudgetClamp(name, requested, principal, global_cap, applied, clamped_by)


def clamp_budget(
    name: str, requested: int | None, principal: int | None, global_cap: int | None
) -> BudgetClamp:
    """Public single-budget clamp (S2d: the pipeline's maxSkills)."""
    return _clamp(name, requested, principal, global_cap)


def effective_budgets(
    principal: AgentPrincipal,
    settings: Settings,
    *,
    requested_tools: int | None = None,
    requested_servers: int | None = None,
) -> Budgets:
    tools = _clamp(
        "maxTools",
        requested_tools,
        principal.max_tools,
        settings.max_exposed_tools,
    )
    servers = _clamp(
        "maxServers",
        requested_servers,
        principal.max_servers,
        settings.max_exposed_servers,
    )
    # A principal row always carries max_tools, so `applied` is never None here.
    max_tools = tools.applied if tools.applied is not None else settings.max_exposed_tools
    return Budgets(max_tools=max_tools, max_servers=servers.applied, clamps=(tools, servers))


def cap_servers(  # noqa: UP047 — PEP 695 syntax trips the 3.10 edit hook
    items: list[T], server_of: Callable[[T], Hashable], max_servers: int | None
) -> list[T]:
    """Keep `items` in order, dropping any item whose server would be the
    (max_servers+1)-th DISTINCT server. None = no cap. `server_of` maps an
    item to its server key."""
    if max_servers is None:
        return list(items)
    seen: set[Hashable] = set()
    out: list[T] = []
    for item in items:
        sid = server_of(item)
        if sid not in seen:
            if len(seen) >= max_servers:
                continue
            seen.add(sid)
        out.append(item)
    return out
