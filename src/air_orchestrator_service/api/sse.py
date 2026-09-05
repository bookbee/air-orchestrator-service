"""Server-Sent Events: framing, heartbeat, and the headers that keep a stream alive.

SSE rather than WebSocket, settling review item 08. A turn is strictly one request in
and one ordered stream out, with no client-to-server messaging mid-turn — confirming a
proposed mutation is a *new* turn citing a proposal id, not a message upstream on an
open socket. SSE survives ordinary HTTP infrastructure (both gateways, proxies,
corporate TLS interception) where WebSocket upgrades routinely do not, and it degrades
to a plain response for a client that does not want it.

The two headers at the bottom are not decoration. Without ``X-Accel-Buffering: no`` an
nginx in front of this service buffers the whole response and delivers every event at
once at the end, which turns a streaming API into a slow non-streaming one — and does
so silently, in exactly the deployment where it matters.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Final

import orjson
from starlette.responses import StreamingResponse

from air_orchestrator_service.observability.logging import get_logger
from air_orchestrator_service.schemas.events import Event

__all__ = ["SSE_MEDIA_TYPE", "event_stream_response", "format_event", "wants_stream"]

_log = get_logger(__name__)

SSE_MEDIA_TYPE: Final[str] = "text/event-stream"

#: Sent when the engine has produced nothing for this long. A comment line, which the
#: EventSource spec requires clients to ignore, so it cannot be mistaken for data.
HEARTBEAT_INTERVAL_S: Final[float] = 15.0
_HEARTBEAT: Final[bytes] = b": heartbeat\n\n"


def wants_stream(accept: str | None) -> bool:
    """Whether this caller asked for the stream.

    Content-negotiated rather than a query parameter or a separate path: it is the
    same turn either way, and one engine feeds both. Absent or unrecognised means
    JSON, so curl and a batch caller get a body rather than a stream they cannot read.
    """
    return accept is not None and SSE_MEDIA_TYPE in accept.lower()


def format_event(event: Event) -> bytes:
    """One event as an SSE frame.

    The ``event:`` name is taken from the model's own discriminator rather than from a
    caller-supplied string, so the wire name and the payload's ``event`` field cannot
    disagree — a client may read either and get the same answer.
    """
    payload = event.model_dump(mode="json", exclude_none=True)
    name = payload["event"]
    body = orjson.dumps(payload).decode("utf-8")
    return f"event: {name}\ndata: {body}\n\n".encode()


async def _with_heartbeat(events: AsyncIterator[Event], *, interval: float) -> AsyncIterator[bytes]:
    """Frame each event, emitting a heartbeat through any long gap.

    The engine is awaited as a task so that a slow stage does not also stall the
    keepalive. The task is cancelled on the way out — including when the client
    disconnects mid-turn, which arrives here as a cancellation — so a dropped
    connection does not leave a turn running with nowhere to send its output.
    """
    iterator = events.__aiter__()
    pending: asyncio.Task[Event] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield _HEARTBEAT
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                pending = None
                return
            finally:
                if pending is not None and pending.done() and pending.exception() is not None:
                    pending = None
            pending = None
            yield format_event(event)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


def event_stream_response(
    events: AsyncIterator[Event], *, interval: float = HEARTBEAT_INTERVAL_S
) -> StreamingResponse:
    """Wrap an engine's events as a streaming HTTP response."""
    return StreamingResponse(
        _with_heartbeat(events, interval=interval),
        media_type=SSE_MEDIA_TYPE,
        headers={
            # A stream is never a cache entry, and an intermediary that stores one
            # would serve a stale conversation to the next caller.
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx and several corporate proxies buffer a response body by default,
            # which delivers the whole turn at once at the end. See the module docstring.
            "X-Accel-Buffering": "no",
        },
    )
