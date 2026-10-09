"""App-wide request body cap, enforced before routing, auth and parsing.

A declared Content-Length over the cap is refused outright with 413. A body
without one (chunked) is read up to the cap, counting bytes as they arrive,
then replayed to the app; one byte over and the request is refused with 413
without reaching a handler. A client that under-declares Content-Length and
then sends more is cut off at the cap: the middleware answers 413 itself (the
app only sees a disconnect, never a truncated body) unless the app already
started its response. Memory per request is bounded by the cap either way.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcprouter.limits import MAX_BODY_BYTES, format_bytes

__all__ = ["MAX_BODY_BYTES", "BodySizeLimitMiddleware"]


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self._detail = json.dumps(
            {"detail": f"request body too large (limit {format_bytes(max_bytes)})"}
        ).encode()

    async def _reject(self, send: Send) -> None:
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(self._detail)).encode()),
        ]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": self._detail})

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
                await self._reject(send)
                return
            await self._counting(scope, receive, send)
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
                await self._reject(send)
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

    async def _counting(self, scope: Scope, receive: Receive, send: Send) -> None:
        """A client that under-declares Content-Length still cannot exceed the
        cap: past it the app reads a disconnect and the client gets the 413."""
        seen = 0
        started = rejected = False

        async def counted() -> Message:
            nonlocal seen, rejected
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    if not started and not rejected:
                        rejected = True
                        await self._reject(send)
                    return {"type": "http.disconnect"}
            return msg

        async def guarded(msg: Message) -> None:
            nonlocal started
            if rejected:
                return  # the 413 is the response; drop whatever the app sends
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        await self.app(scope, counted, guarded)
