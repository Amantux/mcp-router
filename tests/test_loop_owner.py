"""P-602: exactly one process per database runs the background loops."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine

from mcprouter.settings import Settings
from mcprouter.singleton import claim_loop_owner, release_loop_owner

from .conftest import TEST_DB_URL, requires_db

pytestmark = requires_db


def test_two_apps_one_database_exactly_one_owner(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    from mcprouter.api.app import create_app

    a, b = create_app(settings, env={}), create_app(settings, env={})
    try:
        with caplog.at_level(logging.ERROR, logger="mcprouter.singleton"):
            owners = [claim_loop_owner(a.state.engine), claim_loop_owner(b.state.engine)]
        assert owners == [True, False]
        assert "not_loop_owner" in caplog.text
        assert claim_loop_owner(a.state.engine) is True  # re-entrant for the owner
        release_loop_owner(a.state.engine)
        assert claim_loop_owner(b.state.engine) is True  # ownership moves on release
    finally:
        release_loop_owner(a.state.engine)
        release_loop_owner(b.state.engine)


def test_dispose_releases_ownership() -> None:
    first, second = create_engine(TEST_DB_URL), create_engine(TEST_DB_URL)
    try:
        assert claim_loop_owner(first) is True
        assert claim_loop_owner(second) is False
        first.dispose()  # a dead process / engine frees the lock with its session
        assert claim_loop_owner(second) is True
    finally:
        release_loop_owner(first)
        release_loop_owner(second)
        first.dispose()
        second.dispose()
