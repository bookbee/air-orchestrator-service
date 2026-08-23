"""``POST /v1/query`` — the business channel's structured query.

Same engine and same event contract as ``/v1/chat``; what differs is the profile.
A business answer carries ``structured``, validated against the caller's
``output_schema`` by the output guardrail (Phase 2 — the echo engine reports the
schema it was given rather than pretending to enforce it).

Pinned to the business channel by declaration, for the reason given in ``chat.py``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from starlette.responses import StreamingResponse

from air_platform.api.deps import AppState, get_app_state, require_business
from air_platform.api.errors import InsufficientScopeError
from air_platform.api.middleware import TURN_STATUS_ATTR
from air_platform.api.sse import SSE_MEDIA_TYPE, event_stream_response, wants_stream
from air_platform.constants import SCOPE_QUERY_WRITE
from air_platform.engine.echo import EchoEngine, TurnRequest, collect
from air_platform.schemas.chat import QueryRequest, TurnResult
from air_platform.schemas.common import Principal

router = APIRouter(tags=["query"])


@router.post(
    "/query",
    response_model=TurnResult,
    summary="Run a business query",
    description=(
        "Business channel. Answers as structured output alongside the prose. Send "
        "`Accept: text/event-stream` to receive the turn as it happens.\n\n"
        "`output_schema` is a JSON Schema the answer must satisfy; it is enforced by "
        "the output guardrail."
    ),
    responses={200: {"content": {SSE_MEDIA_TYPE: {}, "application/json": {}}}},
)
async def query(
    request: Request,
    response: Response,
    body: QueryRequest,
    state: Annotated[AppState, Depends(get_app_state)],
    principal: Annotated[Principal, require_business],
    accept: Annotated[str | None, Header()] = None,
) -> TurnResult | StreamingResponse:
    if not principal.has_scope(SCOPE_QUERY_WRITE):
        raise InsufficientScopeError(f"This key lacks the '{SCOPE_QUERY_WRITE}' scope.")

    engine = EchoEngine(state.settings, state.sessions)
    events = engine.run(TurnRequest.from_query(body), principal)

    if wants_stream(accept):
        return event_stream_response(events)

    include_trace = body.options.include_trace if body.options else True
    result = await collect(events, include_trace=include_trace)

    setattr(request.state, TURN_STATUS_ATTR, result.status.value)
    response.headers["X-Turn-Status"] = result.status.value
    return result
