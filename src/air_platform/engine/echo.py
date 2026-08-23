"""The Phase 1 engine: the real pipeline shape, with the work stubbed out.

This exists so a client can be integrated against the **actual** contract before the
orchestrator is written. It walks the stages of docs/01-hld.md §4 in order, emits the
event sequence a real turn emits, maintains real session state — and then, instead of
calling air-classifier, air-rag and the model gateway, echoes.

What is real here:

* the event sequence and its ordering, including the ``turn.start``/``turn.end`` frame;
* session creation, the bounded history window, and multi-turn continuity;
* the proposal lifecycle — a mutation is proposed, and only ``confirm.proposal_id``
  executes it, with any other turn cancelling it;
* the channel split: prose on ``customer``, structured output on ``business``;
* option clamping, so a caller can narrow a budget and never widen it.

What is fake: the answer text, and every stage that would call another service. Those
stages report ``skipped`` rather than ``ok``, so a client integrating against this can
tell a stubbed turn from a real one, and so this engine can never be mistaken for
working software in a demo.

Phase 2 replaces :meth:`EchoEngine._answer` and turns the skipped stages real. The
event contract does not change, which is the whole point of building it first.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Final

from air_platform.config import Settings
from air_platform.constants import (
    Channel,
    EventType,
    Route,
    Stage,
    StageStatus,
    TurnStatus,
)
from air_platform.memory.session import InMemorySessionStore
from air_platform.observability import metrics
from air_platform.observability.logging import get_logger, safe_text_fields
from air_platform.schemas.chat import (
    ChatRequest,
    Proposal,
    QueryRequest,
    StageRecord,
    TurnOptions,
    TurnResult,
)
from air_platform.schemas.common import Principal, Usage
from air_platform.schemas.events import (
    AnswerEvent,
    ErrorEvent,
    Event,
    ProposalEvent,
    RouteEvent,
    StageEvent,
    TurnEndEvent,
    TurnStartEvent,
    UsageEvent,
)
from air_platform.schemas.session import PendingProposal, Session, Turn

__all__ = ["EchoEngine", "TurnRequest", "collect"]

_log = get_logger(__name__)

_TURN_ID_PREFIX: Final[str] = "turn_"
_PROPOSAL_ID_PREFIX: Final[str] = "prop_"

#: Saying this in a message asks the echo engine to propose a mutation, so the
#: propose → confirm → execute path can be exercised end to end without air-action.
#: A literal trigger rather than anything inferred: this engine must not appear to
#: understand intent it does not.
PROPOSE_TRIGGER: Final[str] = "/propose"

#: Stages this engine cannot really run. Reported as `skipped` with a reason, never
#: as `ok` — a client must be able to tell a stubbed turn from a real one.
_STUBBED: Final[dict[Stage, str]] = {
    Stage.CACHE: "semantic cache is Phase 5",
    Stage.CLASSIFY: "air-classifier not called by the echo engine",
    Stage.GATHER: "no downstream services called by the echo engine",
}


class TurnRequest:
    """The two route bodies, normalised to what the engine actually needs.

    A small adapter rather than a shared base model: ``ChatRequest.message`` and
    ``QueryRequest.query`` are named for their own callers, and collapsing them into
    one field on the wire would make the OpenAPI worse to read for both.
    """

    __slots__ = ("confirm", "options", "output_schema", "session_id", "text")

    def __init__(
        self,
        *,
        text: str,
        session_id: str | None,
        options: TurnOptions | None,
        confirm: object | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        self.text = text
        self.session_id = session_id
        self.options = options or TurnOptions()
        self.confirm = confirm
        self.output_schema = output_schema

    @classmethod
    def from_chat(cls, body: ChatRequest) -> TurnRequest:
        return cls(
            text=body.message,
            session_id=body.session_id,
            options=body.options,
            confirm=body.confirm,
        )

    @classmethod
    def from_query(cls, body: QueryRequest) -> TurnRequest:
        return cls(
            text=body.query,
            session_id=body.session_id,
            options=body.options,
            output_schema=body.output_schema,
        )


class EchoEngine:
    """Runs a turn as an async generator of events.

    A generator, not a function returning a result: streaming is the native shape and
    the non-streaming route is the adapter (:func:`collect`), rather than the reverse.
    Phase 2's real engine keeps this signature.
    """

    def __init__(self, settings: Settings, sessions: InMemorySessionStore) -> None:
        self._settings = settings
        self._sessions = sessions

    async def run(
        self, request: TurnRequest, principal: Principal
    ) -> AsyncIterator[Event]:
        turn_id = f"{_TURN_ID_PREFIX}{uuid.uuid4().hex}"
        started = time.perf_counter()
        status = TurnStatus.OK

        session = await self._resolve_session(request.session_id, principal)

        yield TurnStartEvent(
            turn_id=turn_id, session_id=session.session_id, channel=principal.channel
        )

        _log.info(
            "turn.start",
            turn_id=turn_id,
            session_id=session.session_id,
            **safe_text_fields(request.text, self._settings),
        )

        try:
            async for event in self._pipeline(turn_id, request, principal, session):
                if isinstance(event, TurnEndEvent):  # pragma: no cover — defensive
                    continue
                if event.event is EventType.ANSWER and event.refusal:
                    status = TurnStatus.REFUSED
                yield event
        except Exception as exc:
            # Headers are long gone, so this cannot be an HTTP error. The code matches
            # the RFC 9457 slug the same failure would carry before the first byte, so
            # a client keeps one error vocabulary rather than two.
            _log.error("turn.failed", turn_id=turn_id, exc_info=exc)
            status = TurnStatus.ERROR
            yield ErrorEvent(
                code="internal-error",
                detail="The turn failed. Quote the request id when reporting.",
                retryable=True,
            )

        elapsed_ms = (time.perf_counter() - started) * 1000
        yield TurnEndEvent(turn_id=turn_id, status=status, latency_ms=round(elapsed_ms, 3))

        metrics.record_turn(
            channel=principal.channel,
            status=status,
            route=Route.DIRECT,
            tenant=principal.tenant,
            duration_s=elapsed_ms / 1000,
            cost_usd=0.0,
        )

    # ── Pipeline ──────────────────────────────────────────────────────────────

    async def _pipeline(
        self,
        turn_id: str,
        request: TurnRequest,
        principal: Principal,
        session: Session,
    ) -> AsyncIterator[Event]:
        async for event in self._stage(Stage.GUARDRAILS_IN):
            yield event

        # A confirmation is resolved before anything else looks at the text, so that
        # the decision to execute depends on the structured field alone and never on
        # what the accompanying message happens to say.
        confirmed = await self._resolve_confirmation(request, session)

        async for event in self._stage(Stage.CONTEXT):
            yield event
        async for event in self._stage(Stage.CACHE):
            yield event
        async for event in self._stage(Stage.CLASSIFY):
            yield event

        async for event in self._stage(Stage.PLAN):
            yield event
        propose = PROPOSE_TRIGGER in request.text.lower() and confirmed is None
        routes = [Route.ACTION] if propose else [Route.DIRECT]
        yield RouteEvent(
            routes=routes,
            reason=(
                f"echo engine: '{PROPOSE_TRIGGER}' present, exercising the proposal path"
                if propose
                else "echo engine: no downstream services are called"
            ),
            capabilities=[],
        )

        async for event in self._stage(Stage.GATHER):
            yield event

        if propose:
            yield ProposalEvent(proposal=await self._propose(session))

        started = time.perf_counter()
        text, structured = self._answer(request, principal, session, confirmed, propose)
        yield StageEvent(
            stage=Stage.SYNTHESISE,
            status=StageStatus.SKIPPED,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            detail="echo engine: no model call",
        )

        async for event in self._stage(Stage.GUARDRAILS_OUT):
            yield event

        yield AnswerEvent(text=text, structured=structured, grounded=False, refusal=False)

        # Persisted after the answer is produced, so a turn that fails mid-pipeline
        # does not leave a half-turn in the history for the next prompt to inherit.
        started = time.perf_counter()
        window = self._settings.turn.window_turns
        session.append(Turn(turn_id=turn_id, role="user", content=request.text), window=window)
        session.append(Turn(turn_id=turn_id, role="assistant", content=text), window=window)
        await self._sessions.save(session)
        yield StageEvent(
            stage=Stage.PERSIST,
            status=StageStatus.OK,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )

        yield UsageEvent(usage=Usage(model_calls=0, cost_usd=0.0, cache_hit=False))

    async def _stage(self, stage: Stage) -> AsyncIterator[Event]:
        """Emit one stage's outcome, skipping what this engine cannot do."""
        started = time.perf_counter()
        detail = _STUBBED.get(stage)
        status = StageStatus.SKIPPED if detail else StageStatus.OK
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        metrics.record_stage(stage=stage, status=status, duration_s=latency_ms / 1000)
        yield StageEvent(stage=stage, status=status, latency_ms=latency_ms, detail=detail)

    # ── Pieces ────────────────────────────────────────────────────────────────

    async def _resolve_session(self, session_id: str | None, principal: Principal) -> Session:
        """Find the caller's session, or start one.

        An unknown or someone else's id starts a *new* session rather than erroring:
        the id is echoed back on ``turn.start``, so a client that lost track picks the
        new one up, and a guessed id reveals nothing about whether it existed.
        """
        if session_id is not None:
            existing = await self._sessions.get(session_id, principal)
            if existing is not None:
                return existing
        return await self._sessions.create(principal)

    async def _resolve_confirmation(
        self, request: TurnRequest, session: Session
    ) -> PendingProposal | None:
        """Apply ``confirm``, and cancel any proposal this turn did not answer.

        The cancellation is the important half (docs/00-plan.md §4 Q3): a proposal
        that survives an unrelated turn is a stale "yes" waiting to happen.
        """
        confirm = request.confirm
        if confirm is None:
            await self._sessions.clear_pending(session)
            return None

        proposal_id = getattr(confirm, "proposal_id", "")
        approved = bool(getattr(confirm, "approve", False))
        pending = await self._sessions.take_pending(session, proposal_id)
        if pending is None:
            metrics.record_proposal(outcome="expired")
            return None
        if not approved:
            metrics.record_proposal(outcome="rejected")
            return None
        metrics.record_proposal(outcome="confirmed")
        return pending

    async def _propose(self, session: Session) -> Proposal:
        pending = PendingProposal.create(
            proposal_id=f"{_PROPOSAL_ID_PREFIX}{uuid.uuid4().hex[:12]}",
            action="echo.noop",
            arguments={"note": "the echo engine proposes nothing that touches a real system"},
            risk="low",
            summary=(
                "A stand-in mutation, so the confirm path can be exercised "
                "before air-action exists."
            ),
            ttl_seconds=self._settings.session.proposal_ttl_seconds,
        )
        await self._sessions.set_pending(session, pending)
        metrics.record_proposal(outcome="created")
        return Proposal(
            proposal_id=pending.proposal_id,
            action=pending.action,
            arguments=pending.arguments,
            risk=pending.risk,
            summary=pending.summary,
            expires_at=pending.expires_at.isoformat(),
        )

    def _answer(
        self,
        request: TurnRequest,
        principal: Principal,
        session: Session,
        confirmed: PendingProposal | None,
        proposed: bool,
    ) -> tuple[str, dict[str, Any] | None]:
        """The echo. Phase 2 replaces this with synthesis through the gateway."""
        turn_number = len(session.turns) // 2 + 1

        if confirmed is not None:
            text = f"Executed {confirmed.action} (echo engine — nothing actually changed)."
        elif proposed:
            text = (
                "That would change something, so here is a proposal rather than an action. "
                "Confirm it by sending the next turn with "
                '`"confirm": {"proposal_id": "…", "approve": true}`.'
            )
        else:
            text = f"echo[{turn_number}]: {request.text}"

        if principal.channel is Channel.BUSINESS:
            # The business channel's contract is structured output, so it gets a body
            # shaped like one even from the stub — a client integrating here should be
            # parsing `structured`, not the prose.
            structured: dict[str, Any] = {
                "echo": request.text,
                "turn": turn_number,
                "session_id": session.session_id,
                "engine": "echo",
            }
            if request.output_schema is not None:
                # Not validated: schema enforcement is the output guardrail's job in
                # Phase 2, and pretending to validate here would be worse than not.
                structured["requested_schema_keys"] = sorted(
                    (request.output_schema.get("properties") or {}).keys()
                )
            return text, structured

        return text, None


async def collect(events: AsyncIterator[Event], *, include_trace: bool) -> TurnResult:
    """Fold an event stream into the non-streaming envelope.

    The two surfaces cannot drift because there is only one engine: this is what a
    caller gets for declining to watch it work.
    """
    result = TurnResult(turn_id="", session_id="", answer="")
    trace: list[StageRecord] = []

    async for event in events:
        match event.event:
            case EventType.TURN_START:
                result.turn_id = event.turn_id
                result.session_id = event.session_id
            case EventType.STAGE:
                trace.append(
                    StageRecord(
                        stage=event.stage,
                        status=event.status,
                        latency_ms=event.latency_ms,
                        detail=event.detail,
                    )
                )
            case EventType.ROUTE:
                result.routes = list(event.routes)
            case EventType.CITATION:
                result.citations.append(event.citation)
            case EventType.PROPOSAL:
                result.proposal = event.proposal
            case EventType.ANSWER:
                result.answer = event.text
                result.structured = event.structured
                result.grounded = event.grounded
                result.refusal = event.refusal
            case EventType.USAGE:
                result.usage = event.usage
            case EventType.ERROR:
                # Surfaced in the envelope rather than raised: the streaming caller got
                # an `error` event and a 200, and the two surfaces must agree.
                result.answer = result.answer or event.detail
            case EventType.TURN_END:
                result.status = event.status

    if include_trace:
        result.trace = trace
    return result
