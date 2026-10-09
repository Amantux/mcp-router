"""Background-loop ownership (wave-6 W0-3 seam; E6 fills it).

``claim_loop_owner`` will decide which replica runs the sync/rollup loops.
For now every process is the owner: behaviour-neutral."""

from __future__ import annotations

from sqlalchemy.engine import Engine


def claim_loop_owner(engine: Engine) -> bool:
    """True when this process should run the background loops. Always True for now."""
    del engine
    return True
