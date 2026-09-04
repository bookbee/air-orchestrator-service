"""The SSE event models — docs/02-lld.md §4.

The stream is the API, so these models *are* the contract, not a rendering of it.
Two properties are load-bearing and are asserted in ``tests/unit/test_events.py``:

* **``turn.start`` and ``turn.end`` always bracket a turn**, including a refused or
  errored one. A client relies on the frame rather than inferring completion from
  silence.
* **A client ignores event names it does not know.** That is what lets
  ``answer.delta`` start arriving once air-infra can stream, with no version bump.

Each event carries its own ``event`` discriminator so that a consumer decoding the
`data:` payload alone — without reading the SSE `event:` line — still knows what it
has. The two always agree; :func:`~air_platform.api.sse.format_event` takes the name
from the model rather than from its caller, so they cannot drift.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from air_platform.constants import Channel, EventType, Route, Stage, StageStatus, TurnStatus
from air_platform.schemas.chat import Citation, Proposal
from air_platform.schemas.common import Usage


class TurnStartEvent(BaseModel):
    """First, always. Carries the session id a new conversation was assigned."""

    event: Literal[EventType.TURN_START] = EventType.TURN_START
    turn_id: str
    session_id: str
    channel: Channel


class StageEvent(BaseModel):
    """One pipeline step finished. The backbone of the stream in v1."""

    event: Literal[EventType.STAGE] = EventType.STAGE
    stage: Stage
    status: StageStatus
    latency_ms: float
    detail: str | None = None


class RouteEvent(BaseModel):
    """What the planner decided, and what it had available to decide from."""

    event: Literal[EventType.ROUTE] = EventType.ROUTE
    routes: list[Route]
    reason: str
    capabilities: list[str] = Field(
        default_factory=list, description="Live capability names the planner could choose among."
    )


class CitationEvent(BaseModel):
    event: Literal[EventType.CITATION] = EventType.CITATION
    citation: Citation


class ProposalEvent(BaseModel):
    """A mutation is proposed. **Nothing has executed.**

    Confirming takes a second turn carrying ``confirm.proposal_id``; this event is
    the only place that id is published.
    """

    event: Literal[EventType.PROPOSAL] = EventType.PROPOSAL
    proposal: Proposal


class AnswerDeltaEvent(BaseModel):
    """Reserved; not emitted in v1.

    Defined now so the contract, the tests and the client-facing docs all describe
    the same shape when air-infra grows a streaming endpoint (docs/01-hld.md §5, §9).
    """

    event: Literal[EventType.ANSWER_DELTA] = EventType.ANSWER_DELTA
    text: str


class AnswerEvent(BaseModel):
    """The terminal answer. ``structured`` is populated on the business channel."""

    event: Literal[EventType.ANSWER] = EventType.ANSWER
    text: str
    structured: dict[str, Any] | None = None
    grounded: bool = False
    refusal: bool = False
    #: True when this turn was handed off to a human rather than fully
    #: answered — `escalation_ref` is the reference a human agent resumes
    #: against (`guardrails/escalation.py`). Distinct from `refusal`: a
    #: refusal is a guardrail declining; an escalation is this service
    #: admitting it cannot fully help.
    escalated: bool = False
    escalation_ref: str | None = None


class UsageEvent(BaseModel):
    event: Literal[EventType.USAGE] = EventType.USAGE
    usage: Usage


class ErrorEvent(BaseModel):
    """A stage failed unrecoverably.

    Not an HTTP error: headers are already on the wire by the time most stages run,
    so this event plus a terminal ``turn.end`` is the only honest way to report it.
    ``code`` matches the RFC 9457 slug the same failure would carry before the first
    byte, so a client has one error vocabulary rather than two.
    """

    event: Literal[EventType.ERROR] = EventType.ERROR
    code: str
    detail: str
    retryable: bool = False


class TurnEndEvent(BaseModel):
    """Last, always — including after an error."""

    event: Literal[EventType.TURN_END] = EventType.TURN_END
    turn_id: str
    status: TurnStatus
    latency_ms: float


#: Every event the stream can carry, discriminated on ``event``.
Event = Annotated[
    TurnStartEvent
    | StageEvent
    | RouteEvent
    | CitationEvent
    | ProposalEvent
    | AnswerDeltaEvent
    | AnswerEvent
    | UsageEvent
    | ErrorEvent
    | TurnEndEvent,
    Field(discriminator="event"),
]
