"""App-wide request body cap: rejected before parse and before auth."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.types import Message, Receive, Scope, Send

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


# ---------------------------------------------------------- ASGI level (HS-C-015)
def _asgi_call(
    headers: list[tuple[bytes, bytes]], chunks: list[bytes]
) -> tuple[list[int], list[bytes]]:
    """Drive BodySizeLimitMiddleware around a handler that reads the whole body
    (as any FastAPI route does). Returns (statuses sent, bodies the handler saw)."""
    import asyncio

    from starlette.requests import ClientDisconnect, Request
    from starlette.responses import JSONResponse

    from mcprouter.api.body_limit import BodySizeLimitMiddleware

    seen: list[bytes] = []

    async def handler(scope: Scope, receive: Receive, send: Send) -> None:
        try:
            body = await Request(scope, receive).body()
        except ClientDisconnect:
            return
        seen.append(body)
        await JSONResponse({"ok": True})(scope, receive, send)

    pending = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]

    async def receive() -> Message:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    statuses: list[int] = []

    async def send(msg: Message) -> None:
        if msg["type"] == "http.response.start":
            statuses.append(msg["status"])

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": headers}
    asyncio.run(BodySizeLimitMiddleware(handler)(scope, receive, send))
    return statuses, seen


def test_declared_length_over_cap_is_413_before_the_handler() -> None:
    statuses, seen = _asgi_call([(b"content-length", str(MIB + 1).encode())], [b"x"])
    assert (statuses, seen) == ([413], [])


def test_under_declared_length_overflow_is_413_not_a_truncated_body() -> None:
    """A client that declares a small Content-Length and then streams past the
    cap gets a 413; the handler never runs on a truncated body."""
    chunk = b"x" * (512 * 1024)
    statuses, seen = _asgi_call([(b"content-length", b"10")], [chunk, chunk, chunk])
    assert (statuses, seen) == ([413], [])


def test_body_at_the_cap_reaches_the_handler() -> None:
    body = b"x" * MIB
    statuses, seen = _asgi_call([(b"content-length", str(MIB).encode())], [body])
    assert (statuses, seen) == ([200], [body])
