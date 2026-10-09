"""Shared helpers moved out of tests/test_edge_roundtrip.py (W0-2); not a test module."""

from __future__ import annotations

import dataclasses

from sqlalchemy import delete

from mcprouter.db import make_engine, make_session_factory
from mcprouter.models import AgentPrincipal
from mcprouter.settings import Settings

KEY = "edge-test-key-0123456789abcdef"


def _drop_principal(settings: Settings) -> None:
    """The bootstrapped principal would take later tests out of dev mode."""
    eng = make_engine(settings)
    try:
        with make_session_factory(eng)() as session:
            session.execute(delete(AgentPrincipal).where(AgentPrincipal.agent_id == "edgebot"))
            session.commit()
    finally:
        eng.dispose()


def _settings(**kw: object) -> Settings:
    base = Settings.from_env()
    return dataclasses.replace(base, agent_keys=f"edgebot:{KEY}", **kw)  # type: ignore[arg-type]
