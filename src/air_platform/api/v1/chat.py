"""``POST /v1/chat`` — the customer channel's conversational turn.

Pinned to the customer channel by ``require_customer`` rather than branching on the
principal inside a shared handler. Two paths pinned by declaration is what stops a
later edit from applying one channel's guardrail profile to the other's traffic.

Streaming is content-negotiated: ``Accept: text/event-stream`` streams, anything else
returns the terminal :class:`TurnResult` as one JSON body. Same engine either way.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from starlette.responses import StreamingResponse

from air_platform.api.deps import AppState, get_app_state, require_customer
from air_platform.api.errors import InsufficientScopeError, MalformedRequestError
from air_platform.api.middleware import TURN_STATUS_ATTR
from air_platform.api.sse import SSE_MEDIA_TYPE, event_stream_response, wants_stream
from air_platform.constants import SCOPE_CHAT_WRITE, Channel
from air_platform.engine.echo import EchoEngine, TurnRequest, collect
from air_platform.schemas.chat import ChatRequest, TurnResult
from air_platform.schemas.common import Principal

router = APIRouter(tags=["chat"])


def reject_privileged_options(body_options: object, principal: Principal) -> None:
    """Refuse an option this caller's channel may not set.

    Refused, not ignored. ``redact_pii`` is configurable on the business channel
    because internal analysts legitimately query customer records; a customer-channel
    caller who set it and was quietly ignored would believe they had turned redaction
    off, which is a worse outcome than an error either way.
    """
    if body_options is None:
        return
    redact = getattr(body_options, "redact_pii", None)
    if redact is not None and principal.channel is Channel.CUSTOMER:
        raise MalformedRequestError(
            "options.redact_pii cannot be set on the customer channel; "
            "redaction is unconditional there."
        )


@router.post(
    "/chat",
    response_model=TurnResult,
    summary="Send a conversational turn",
    description=(
        "Customer channel. Send `Accept: text/event-stream` to receive the turn as it "
        "happens; any other Accept returns the completed result as JSON.\n\n"
        "A turn that warrants a mutation returns a `proposal` and changes nothing — "
        "execute it by sending the next turn with `confirm.proposal_id`."
    ),
    responses={200: {"content": {SSE_MEDIA_TYPE: {}, "application/json": {}}}},
)
async def chat(
    request: Request,
    response: Response,
    body: ChatRequest,
    state: Annotated[AppState, Depends(get_app_state)],
    principal: Annotated[Principal, require_customer],
    accept: Annotated[str | None, Header()] = None,
) -> TurnResult | StreamingResponse:
    if not principal.has_scope(SCOPE_CHAT_WRITE):
        raise InsufficientScopeError(f"This key lacks the '{SCOPE_CHAT_WRITE}' scope.")
    reject_privileged_options(body.options, principal)

    engine = EchoEngine(state.settings, state.sessions)
    events = engine.run(TurnRequest.from_chat(body), principal)

    if wants_stream(accept):
        return event_stream_response(events)

    include_trace = body.options.include_trace if body.options else True
    result = await collect(events, include_trace=include_trace)

    # The turn's own outcome, for the access log. A streaming response cannot report
    # this in its status line, so the log reads it from here; the non-streaming path
    # sets it too, so one dashboard query covers both surfaces.
    setattr(request.state, TURN_STATUS_ATTR, result.status.value)
    response.headers["X-Turn-Status"] = result.status.value
    return result
