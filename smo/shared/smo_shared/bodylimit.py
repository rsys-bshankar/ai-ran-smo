"""Request body size cap (PR-SEC-8.1).

A caller that sends a body larger than the cap is answered 413 before the service reads it: from the
`Content-Length` header when there is one, and by counting bytes as they arrive when there is not (chunked
uploads), stopping the read at the cap. Without a cap one request can make a service buffer as much memory as the
caller cares to send.

    app.add_middleware(BodySizeLimit, settings=lambda: (default_bytes, {"/mlmr/models/*/artifact": 50 * MIB}))

`settings` is called per request, so a limit can come from the environment at run time. Overrides match the
request path with `fnmatch`, `*` standing for anything (use it for one path segment: `/a/*/b`); the first match wins,
and a request matching none gets the default. The body of a streamed request that goes over is not answered
until the service next reads it; the middleware then ends the request with 413 as long as no response has
started.
"""

import fnmatch
import json
import os
from collections.abc import Callable

from .correlation import get_correlation_id

MIB = 1024 * 1024

Settings = Callable[[], tuple[int, dict[str, int]]]


def parse_overrides(text: str) -> dict[str, int]:
    """`path-pattern=bytes,path-pattern=bytes` -> {pattern: bytes}."""
    overrides: dict[str, int] = {}
    for item in filter(None, (part.strip() for part in text.split(","))):
        pattern, _, size = item.rpartition("=")
        if not pattern or not size.strip().isdigit():
            raise ValueError(f"bad body-limit override {item!r}: expected <path-pattern>=<bytes>")
        overrides[pattern.strip()] = int(size)
    return overrides


def settings_from_env(prefix: str, default_overrides: str = "", default_bytes: int = MIB) -> Settings:
    """`<prefix>_MAX_BODY_BYTES` and `<prefix>_MAX_BODY_OVERRIDES`, read on every request."""
    def read() -> tuple[int, dict[str, int]]:
        return (int(os.environ.get(f"{prefix}_MAX_BODY_BYTES", default_bytes)),
                parse_overrides(os.environ.get(f"{prefix}_MAX_BODY_OVERRIDES", default_overrides)))
    return read


# Raised from the counting `receive` wrapper when a streamed body passes the cap; caught in `BodySizeLimit.__call__`, never seen by the application.
class _TooLarge(Exception):
    pass


class BodySizeLimit:
    """ASGI middleware that answers 413 PAYLOAD_TOO_LARGE for a request body over the cap (see the module description for how the cap is chosen).

    A plain ASGI middleware: it wraps `receive` to count bytes and `send` to see whether a response has started. Only `http` scopes are limited
    (websocket and lifespan pass through).
    """
    def __init__(self, app, settings: Settings):
        self.app = app
        self.settings = settings

    def _limit_for(self, path: str) -> int:
        """Returns the cap in bytes for `path`: the first override whose pattern matches (`fnmatch`, case-sensitive), else the default.

        `settings()` is called here, per request, so a limit changed in the environment applies without a restart.
        """
        default, overrides = self.settings()
        for pattern, size in overrides.items():
            if fnmatch.fnmatchcase(path, pattern):
                return size
        return default

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self._limit_for(scope["path"])
        # Cheap early refusal: a declared Content-Length over the cap is answered before the application reads a byte. A missing or non-numeric header
        # falls through to the byte count below.
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            return await self._refuse(send, limit)

        received = 0
        started = False

        # Chunked uploads have no Content-Length, so the bytes are counted as the application reads them; the read is cut off by raising _TooLarge at
        # the cap.
        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _TooLarge:
            # Once the application has begun its response (http.response.start sent) a 413 can no longer be sent, so the request is left to end as it
            # is.
            if not started:
                await self._refuse(send, limit)

    @staticmethod
    async def _refuse(send, limit: int) -> None:
        """Sends the 413 answer (a small JSON body with `title`, `status`, `detail`, and `Connection: close`) straight on `send`.

        Built by hand because it runs in the middleware, ahead of the application's exception handlers.
        """
        body = json.dumps({"title": "PAYLOAD_TOO_LARGE", "status": 413,
                           "detail": f"the request body is larger than the {limit} bytes this route accepts",
                           "correlationId": get_correlation_id()}).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})
