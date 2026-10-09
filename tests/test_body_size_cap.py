"""App-wide request body cap: rejected before parse and before auth."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from mcprouter.api import routes_decision
from tests.support.edge_app import PATH, edge_client

MIB = 1024 * 1024


@pytest.fixture
def parse_spy(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []
    monkeypatch.setattr(routes_decision.serve, "parse_questions", calls.append)
    return calls


def test_oversized_content_length_is_413_without_auth(parse_spy: list[object]) -> None:
    with edge_client() as (_, client):
        r = client.post(
            PATH, content=b"x" * (2 * MIB), headers={"content-type": "application/json"}
        )
    assert r.status_code == 413
    assert r.json() == {"detail": "request body too large (limit 1 MiB)"}
    assert parse_spy == []


def test_oversized_chunked_body_is_413(parse_spy: list[object]) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(20):
            yield b"x" * (128 * 1024)

    with edge_client() as (_, client):
        r = client.post(PATH, content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert parse_spy == []


def test_small_body_still_reaches_the_app() -> None:
    with edge_client() as (_, client):
        assert client.post(PATH, json={"state": "s", "questions": {}}).status_code == 401
