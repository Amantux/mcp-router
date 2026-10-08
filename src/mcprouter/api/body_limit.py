"""App-wide request body cap, enforced before routing, auth and parsing.

A declared Content-Length over the cap is refused outright. A body without one
(chunked) is read up to the cap, counting bytes as they arrive, then replayed to
the app; one byte over and the request is refused without reaching a handler.
Memory per request is therefore bounded by the cap either way.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 1024 * 1024
_DETAIL = json.dumps({"detail": "request body too large (limit 1 MiB)"}).encode()


async def _reject(send: Send) -> None:
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(_DETAIL)).encode()),
    ]
    await send({"type": "http.response.start", "status": 413, "headers": headers})
    await send({"type": "http.response.body", "body": _DETAIL})


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None:
            try:
                too_big = int(length) > self.max_bytes
            except ValueError:
                too_big = True  # malformed length: refuse rather than guess
            if too_big:
                await _reject(send)
                return
            await self.app(scope, self._counting(receive), send)
            return
        # No declared length: buffer up to the cap, then replay.
        chunks: list[bytes] = []
        size = 0
        while True:
            msg = await receive()
            if msg["type"] != "http.request":
                break  # disconnect: let the app see it on its own receive
            size += len(msg.get("body", b""))
            if size > self.max_bytes:
                await _reject(send)
                return
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    def _counting(self, receive: Receive) -> Receive:
        """A client that under-declares Content-Length still cannot exceed the cap."""
        seen = 0

        async def wrapped() -> Message:
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    return {"type": "http.disconnect"}
            return msg

        return wrapped
