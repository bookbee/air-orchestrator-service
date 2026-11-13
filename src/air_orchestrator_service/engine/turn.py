"""The turn engine: the pipeline in docs/01-hld.md §4, as an async generator.

``TurnEngine.run()`` yields :mod:`~air_orchestrator_service.schemas.events`
models in pipeline order. Streaming is the native shape and the non-streaming
route is the adapter (:func:`collect`), rather than the reverse — a turn that
fans out to several services takes seconds, and the client must be able to
render progress before the answer exists.

**What this engine actually does today.** Guardrails (injection screening, a
scope guard, PII masking in both directions, an escalation path), the prompt
registry, and synthesis through air-model-gateway are real. So are the ceilings: a
turn-level deadline computed once and decremented before the synthesis call —
bound to that call's own timeout, and checked *before* a call is made so an
exhausted budget skips the call rather than starting it — plus per-turn and
per-session cost ceilings.

**What is stubbed, and why it says so.** ``CACHE`` (Phase 5) and ``GATHER``
(Phase 3, pending clients for air-rag/air-tools) report ``StageStatus.SKIPPED``
with a reason, never ``ok`` — see ``_STUBBED``. A client
must always be able to tell a stubbed turn from a real one, and this engine must
never be mistaken for a complete one in a demo. Routing is still the literal
``/propose`` trigger for the same reason: with ``GATHER`` stubbed there is
nothing to route to besides a direct answer, and a stand-in must not appear to
understand intent it does not.

**Status vocabulary is load-bearing.** ``TurnStatus.DEGRADED`` covers a provider
failure — the model gateway being slow, down, or refusing this service's
credentials is the system coping, and the turn still answers; the degraded stage
names the gateway so ``TurnResult.degraded`` says what was missing.
``TurnStatus.REFUSED`` covers a refusal, whether this service's own guardrail or
the model provider's safety layer said no — the gateway's ``refusal`` flag is a
verdict, not an empty answer. ``TurnStatus.ERROR`` is reserved for genuinely
unexpected exceptions. Collapsing any two would make an outage, a policy decision
and a bug indistinguishable on a dashboard.

Content that did not originate in this service's own code is demarcated by
``guardrails/boundary.py`` before it reaches a prompt, and the prompt version
used is logged with the turn.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Final

from air_orchestrator_service.clients.classifier import MIN_BUDGET_MS as MIN_CLASSIFY_BUDGET_MS
from air_orchestrator_service.clients.classifier import (
    ClassifierClient,
    ClassifierError,
    label_slug,
)
from air_orchestrator_service.clients.model_gateway import (
    GenerateResult,
    ModelGatewayClient,
    ModelGatewayError,
    StreamDelta,
)
from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import (
    Channel,
    DownstreamService,
    EscalationReason,
    EventType,
    Route,
    Stage,
    StageStatus,
    TurnStatus,
)
from air_orchestrator_service.guardrails.boundary import delimit
from air_orchestrator_service.guardrails.escalation import wants_human
from air_orchestrator_service.guardrails.injection import screen_input
from air_orchestrator_service.guardrails.pii import StreamRedactor, mask_input, scan_output
from air_orchestrator_service.guardrails.scope import screen_scope
from air_orchestrator_service.memory.session import InMemorySessionStore
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.observability.logging import get_logger, safe_text_fields
from air_orchestrator_service.prompts.registry import PromptRegistry
from air_orchestrator_service.schemas.chat import (
    ChatRequest,
    Proposal,
    QueryRequest,
    StageRecord,
    TurnOptions,
    TurnResult,
)
from air_orchestrator_service.schemas.common import Principal, Usage
from air_orchestrator_service.schemas.events import (
    AnswerDeltaEvent,
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
from air_orchestrator_service.schemas.session import PendingProposal, Session, Turn

__all__ = ["TurnEngine", "TurnRequest", "collect"]

_log = get_logger(__name__)

_TURN_ID_PREFIX: Final[str] = "turn_"
_PROPOSAL_ID_PREFIX: Final[str] = "prop_"
_ESCALATION_ID_PREFIX: Final[str] = "esc_"

#: Saying this in a message asks the engine to propose a mutation, so the
#: propose -> confirm -> execute path can be exercised end to end before
#: air-action exists (Phase 4). See the module docstring on why routing
#: itself stays a stand-in this pass.
PROPOSE_TRIGGER: Final[str] = "/propose"

#: Stages this engine still cannot really run. `SYNTHESISE` is not here any
#: more — see the module docstring.
_STUBBED: Final[dict[Stage, str]] = {
    Stage.CACHE: "semantic cache is Phase 5",
    Stage.GATHER: "no downstream services called yet (Phase 3)",
}

_SYNTHESIS_MAX_TOKENS: Final[int] = 800

_INJECTION_REFUSAL: Final[str] = (
    "I can't help with that request — it looked like an attempt to change "
    "how I'm instructed to behave, rather than a question. Rephrase it and "
    "I'll try again."
)
#: The model provider's safety layer declined. Fixed text rather than the
#: gateway's content, which is empty on a refusal (and, streamed, may be partial).
_PROVIDER_REFUSAL: Final[str] = (
    "I'm not able to help with that one. If you think that's a mistake, try "
    "rephrasing, or ask to speak with a person."
)
_DEGRADED_ANSWER: Final[str] = (
    "I'm having trouble putting together a full answer right now. Please try "
    "again in a moment — nothing about your account or order has changed."
)
_SESSION_BUDGET_EXHAUSTED: Final[str] = (
    "This conversation has reached its budget for now. Please start a new "
    "session, or reach out to support if you need to continue this one."
)


class TurnRequest:
    """The two route bodies, normalised to what the engine actually needs."""

    __slots__ = ("confirm", "options", "output_schema", "session_id", "stream", "text")

    def __init__(
        self,
        *,
        text: str,
        session_id: str | None,
        options: TurnOptions | None,
        confirm: object | None = None,
        output_schema: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> None:
        self.text = text
        self.session_id = session_id
        self.options = options or TurnOptions()
        self.confirm = confirm
        self.output_schema = output_schema
        #: The caller is reading Server-Sent Events, so the prose answer is
        #: streamed as `answer.delta` (spec 0004). A JSON caller's turn makes the
        #: non-streaming call, which keeps the gateway's full failover.
        self.stream = stream

    @classmethod
    def from_chat(cls, body: ChatRequest, *, stream: bool = False) -> TurnRequest:
        return cls(
            text=body.message,
            session_id=body.session_id,
            options=body.options,
            confirm=body.confirm,
            stream=stream,
        )

    @classmethod
    def from_query(cls, body: QueryRequest, *, stream: bool = False) -> TurnRequest:
        return cls(
            text=body.query,
            session_id=body.session_id,
            options=body.options,
            output_schema=body.output_schema,
            stream=stream,
        )


class TurnEngine:
    """Runs a turn as an async generator of events. Phase 3 replaces `_gather`
    (currently absent — GATHER is still stubbed) without changing this
    signature."""

    def __init__(
        self,
        settings: Settings,
        sessions: InMemorySessionStore,
        model_gateway: ModelGatewayClient,
        classifier: ClassifierClient | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._gateway = model_gateway
        #: ``None`` when air-classifier is not configured (spec 0005).
        self._classifier = classifier
        self._prompts = PromptRegistry(settings)
        #: Set by `_pipeline` when it degrades a turn — an async generator has
        #: no return value, so this is how `run()` learns the outcome once
        #: iteration finishes, the same way it already reads `refusal` off
        #: the `AnswerEvent` for `TurnStatus.REFUSED`. One `TurnEngine` is
        #: built fresh per request (`api/v1/chat.py`), so there is no
        #: cross-request state here to worry about.
        self._last_status: TurnStatus = TurnStatus.OK

    async def run(self, request: TurnRequest, principal: Principal) -> AsyncIterator[Event]:
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
            async for event in self._pipeline(turn_id, request, principal, session, started):
                if isinstance(event, TurnEndEvent):  # pragma: no cover — defensive
                    continue
                if event.event is EventType.ANSWER and event.refusal:
                    status = TurnStatus.REFUSED
                yield event
            if status is TurnStatus.OK:
                status = self._last_status
        except Exception as exc:
            # Genuinely unexpected — anything this engine anticipates (a slow
            # or unavailable model, an exhausted budget) is handled inside
            # `_pipeline` and degrades instead of raising. Reaching here means
            # something outside that anticipated set broke.
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
        turn_started: float,
    ) -> AsyncIterator[Event]:
        self._last_status = TurnStatus.OK
        deadline_s = self._deadline_seconds(request)

        redact = self._should_redact(request, principal)
        mask = mask_input(request.text) if redact else None
        masked_text = mask.masked_text if mask is not None else request.text

        # ── GUARDRAILS_IN: injection, then scope (only checked if the first
        # already passed — no need to classify scope on text already refused) ──
        guard_started = time.perf_counter()
        injection_verdict = screen_input(masked_text)
        scope_verdict = (
            None
            if injection_verdict.blocked
            else screen_scope(
                masked_text, competitor_names=self._settings.guardrails.competitor_names
            )
        )
        blocked = injection_verdict.blocked or bool(scope_verdict and scope_verdict.blocked)

        detail: str | None = None
        if injection_verdict.blocked:
            detail = f"prompt_injection: {injection_verdict.matched_text!r}"
            metrics.record_guardrail_block(
                direction="in", rule="prompt_injection", channel=principal.channel
            )
        elif scope_verdict and scope_verdict.blocked:
            detail = f"{scope_verdict.category}: {scope_verdict.matched_text!r}"
            metrics.record_guardrail_block(
                direction="in", rule=scope_verdict.category or "scope", channel=principal.channel
            )
        yield StageEvent(
            stage=Stage.GUARDRAILS_IN,
            status=StageStatus.BLOCKED if blocked else StageStatus.OK,
            latency_ms=round((time.perf_counter() - guard_started) * 1000, 3),
            detail=detail,
        )

        if injection_verdict.blocked:
            # No `self._last_status` assignment here: `run()` already derives
            # `TurnStatus.REFUSED` from the `AnswerEvent.refusal` flag below.
            async for event in self._skip_to_answer(
                turn_id,
                masked_text,
                principal,
                session,
                text=_INJECTION_REFUSAL,
                refusal=True,
                route_reason="blocked before routing",
                skip_detail="turn blocked upstream",
            ):
                yield event
            return
        if scope_verdict and scope_verdict.blocked:
            async for event in self._skip_to_answer(
                turn_id,
                masked_text,
                principal,
                session,
                # `response` is always set when `blocked` is True (every
                # branch in `screen_scope` sets it) — the fallback is for
                # the type checker, not a reachable case.
                text=scope_verdict.response or "I'm not able to help with that.",
                route_reason=f"scope guard: {scope_verdict.category}",
                skip_detail="turn blocked upstream",
            ):
                yield event
            return

        # ── Escalation: the one trigger buildable today. The other three
        # (constants.EscalationReason) need Phase 3 capabilities. ──
        if wants_human(masked_text):
            ref = f"{_ESCALATION_ID_PREFIX}{uuid.uuid4().hex[:12]}"
            _log.info(
                "escalation.created",
                turn_id=turn_id,
                session_id=session.session_id,
                reason=EscalationReason.EXPLICIT_REQUEST.value,
                escalation_ref=ref,
            )
            metrics.record_escalation(reason=EscalationReason.EXPLICIT_REQUEST)
            text = (
                "I'm not able to fully help with that myself, so I'm connecting you "
                f"with a team member — reference {ref}. They'll be able to see this "
                "conversation and pick up from here rather than starting over."
            )
            async for event in self._skip_to_answer(
                turn_id,
                masked_text,
                principal,
                session,
                text=text,
                escalated=True,
                escalation_ref=ref,
                route_reason="escalated to a human",
                skip_detail="turn escalated",
            ):
                yield event
            return

        # ── Session cost ceiling: checked before any more work, same reason
        # the guardrails above are — no point running stages for a turn that
        # cannot be answered. ──
        ceiling = self._settings.session.max_cost_usd
        if ceiling is not None and session.total_cost_usd >= ceiling:
            self._last_status = TurnStatus.DEGRADED
            async for event in self._skip_to_answer(
                turn_id,
                masked_text,
                principal,
                session,
                text=_SESSION_BUDGET_EXHAUSTED,
                route_reason="session cost ceiling reached",
                skip_detail="session budget exhausted",
            ):
                yield event
            return

        # A confirmation is resolved before anything else looks at the text, so the
        # decision to execute depends on the structured field alone, never on what
        # the accompanying message happens to say.
        confirmed = await self._resolve_confirmation(request, session)

        async for event in self._stage(Stage.CONTEXT):
            yield event
        async for event in self._stage(Stage.CACHE):
            yield event
        async for event in self._classify(
            masked_text, principal, deadline_at=turn_started + deadline_s
        ):
            yield event

        async for event in self._stage(Stage.PLAN):
            yield event
        propose = PROPOSE_TRIGGER in masked_text.lower() and confirmed is None
        routes = [Route.ACTION] if propose else [Route.DIRECT]
        yield RouteEvent(
            routes=routes,
            reason=(
                f"'{PROPOSE_TRIGGER}' present, exercising the proposal path"
                if propose
                else "no downstream services are wired yet (Phase 3); direct answer only"
            ),
            capabilities=[],
        )

        async for event in self._stage(Stage.GATHER):
            yield event

        if propose:
            yield ProposalEvent(proposal=await self._propose(session))

        # ── Deadline check, right before the expensive part. A canned
        # confirm/propose reply never calls the model, so the deadline is
        # irrelevant to it — only a real synthesis call is bounded. ──
        needs_model = confirmed is None and not propose
        remaining_s = deadline_s - (time.perf_counter() - turn_started)

        if needs_model and remaining_s <= 0:
            self._last_status = TurnStatus.DEGRADED
            yield StageEvent(
                stage=Stage.SYNTHESISE,
                status=StageStatus.DEGRADED,
                latency_ms=0.0,
                detail="turn deadline exceeded before the model call",
            )
            async for event in self._finish_with_answer(
                turn_id, masked_text, principal, session, text=_DEGRADED_ANSWER
            ):
                yield event
            return

        synth_started = time.perf_counter()
        outcome = _Synthesis()
        try:
            async for delta in self._answer(
                request,
                masked_text,
                principal,
                session,
                confirmed,
                propose,
                outcome,
                deadline_at=turn_started + deadline_s if needs_model else None,
            ):
                yield delta
        except TimeoutError:
            # The turn's own deadline ran out while the answer streamed (spec 0004,
            # REQ-STRM-006). Ours, not the gateway's: no `downstream` is named.
            self._last_status = TurnStatus.DEGRADED
            yield StageEvent(
                stage=Stage.SYNTHESISE,
                status=StageStatus.DEGRADED,
                latency_ms=round((time.perf_counter() - synth_started) * 1000, 3),
                detail="turn deadline reached while the answer was streaming",
            )
            async for event in self._finish_with_answer(
                turn_id, masked_text, principal, session, text=_DEGRADED_ANSWER
            ):
                yield event
            return
        except ModelGatewayError as exc:
            self._last_status = TurnStatus.DEGRADED
            yield StageEvent(
                stage=Stage.SYNTHESISE,
                status=StageStatus.DEGRADED,
                latency_ms=round((time.perf_counter() - synth_started) * 1000, 3),
                # `str(exc)` is safe by construction: the client builds it from a
                # status code and a slug-checked problem code, never a URL or body.
                detail=f"model gateway failed (retryable={exc.retryable}): {exc}",
                downstream=DownstreamService.MODEL_GATEWAY,
            )
            async for event in self._finish_with_answer(
                turn_id, masked_text, principal, session, text=_DEGRADED_ANSWER
            ):
                yield event
            return

        # A provider refusal is `blocked`, like a guardrail of our own refusing —
        # the system working, not coping — so it never reads as an outage.
        detail = "model provider refused" if outcome.refused else None
        if detail is None and outcome.prompt_version:
            detail = f"prompt={outcome.prompt_version}"
            if outcome.streamed:
                detail += " streamed"
        yield StageEvent(
            stage=Stage.SYNTHESISE,
            status=StageStatus.BLOCKED if outcome.refused else StageStatus.OK,
            latency_ms=round((time.perf_counter() - synth_started) * 1000, 3),
            detail=detail,
        )
        # The answer event carries the *complete* text, scanned whole by
        # `_finish_with_answer` — so it is authoritative over any deltas: equal to
        # their concatenation on a normal turn, the refusal text on a refusal.
        async for event in self._finish_with_answer(
            turn_id,
            masked_text,
            principal,
            session,
            text=outcome.text,
            structured=outcome.structured,
            usage=outcome.usage,
            refusal=outcome.refused,
        ):
            yield event

    async def _skip_to_answer(
        self,
        turn_id: str,
        masked_text: str,
        principal: Principal,
        session: Session,
        *,
        text: str,
        refusal: bool = False,
        escalated: bool = False,
        escalation_ref: str | None = None,
        route_reason: str,
        skip_detail: str,
    ) -> AsyncIterator[Event]:
        """The shared shape behind a blocked, escalated, or budget-exhausted
        turn: skip `CONTEXT`..`GATHER`, route to nothing, skip `SYNTHESISE`,
        then the normal answer tail. `GUARDRAILS_IN` is emitted by the
        caller, since its status differs by circumstance (`BLOCKED` vs `OK`).
        """
        for stage in (Stage.CONTEXT, Stage.CACHE, Stage.CLASSIFY, Stage.PLAN, Stage.GATHER):
            yield StageEvent(
                stage=stage, status=StageStatus.SKIPPED, latency_ms=0.0, detail=skip_detail
            )
        yield RouteEvent(routes=[], reason=route_reason, capabilities=[])
        yield StageEvent(
            stage=Stage.SYNTHESISE, status=StageStatus.SKIPPED, latency_ms=0.0, detail=skip_detail
        )
        async for event in self._finish_with_answer(
            turn_id,
            masked_text,
            principal,
            session,
            text=text,
            refusal=refusal,
            escalated=escalated,
            escalation_ref=escalation_ref,
        ):
            yield event

    async def _finish_with_answer(
        self,
        turn_id: str,
        masked_text: str,
        principal: Principal,
        session: Session,
        *,
        text: str,
        structured: dict[str, Any] | None = None,
        refusal: bool = False,
        escalated: bool = False,
        escalation_ref: str | None = None,
        usage: Usage | None = None,
    ) -> AsyncIterator[Event]:
        """`GUARDRAILS_OUT` -> `ANSWER` -> `PERSIST` -> `USAGE`: the shared tail
        every turn shape ends with, whichever path produced `text` — a real
        synthesis, a canned refusal, an escalation, or a degraded fallback."""
        out_verdict = scan_output(text)
        if out_verdict.flagged:
            metrics.record_guardrail_block(
                direction="out", rule=",".join(out_verdict.categories), channel=principal.channel
            )
        redacted = ", ".join(out_verdict.categories)
        out_detail = f"redacted: {redacted}" if out_verdict.flagged else None
        yield StageEvent(
            stage=Stage.GUARDRAILS_OUT, status=StageStatus.OK, latency_ms=0.0, detail=out_detail
        )
        text = out_verdict.safe_text

        yield AnswerEvent(
            text=text,
            structured=structured,
            grounded=False,
            refusal=refusal,
            escalated=escalated,
            escalation_ref=escalation_ref,
        )

        started = time.perf_counter()
        window = self._settings.turn.window_turns
        session.append(Turn(turn_id=turn_id, role="user", content=masked_text), window=window)
        session.append(Turn(turn_id=turn_id, role="assistant", content=text), window=window)
        resolved_usage = usage or Usage(model_calls=0, cost_usd=0.0, cache_hit=False)
        session.total_cost_usd += resolved_usage.cost_usd
        await self._sessions.save(session)
        yield StageEvent(
            stage=Stage.PERSIST,
            status=StageStatus.OK,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        yield UsageEvent(usage=resolved_usage)

    async def _stage(self, stage: Stage) -> AsyncIterator[Event]:
        """Emit one stage's outcome, skipping what this engine still cannot do."""
        started = time.perf_counter()
        detail = _STUBBED.get(stage)
        status = StageStatus.SKIPPED if detail else StageStatus.OK
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        metrics.record_stage(stage=stage, status=status, duration_s=latency_ms / 1000)
        yield StageEvent(stage=stage, status=status, latency_ms=latency_ms, detail=detail)

    async def _classify(
        self, masked_text: str, principal: Principal, *, deadline_at: float
    ) -> AsyncIterator[Event]:
        """The ``CLASSIFY`` stage (spec 0005): one stage event, whatever happens.

        The verdict is observed, not yet acted on: it reaches the stage detail and
        the log, and nothing downstream reads it (``REQ-CLS-011``/``012`` are
        deferred). So every failure here is soft by construction. The stage says
        what went wrong and the turn's status is untouched (HLD §7, "Turn
        unaffected"). The customer channel only — see the spec's §3 on why a
        business query is not classified.
        """
        started = time.perf_counter()
        status, detail, downstream = StageStatus.SKIPPED, None, None
        if self._classifier is None:
            detail = "air-classifier is not configured"
        elif principal.channel is not Channel.CUSTOMER:
            detail = "not used on the business channel"
        else:
            bound_s = self._classifier.bound_s(_remaining(deadline_at))
            if bound_s * 1000 < MIN_CLASSIFY_BUDGET_MS:
                # Ours, not the classifier's: no `downstream` is named.
                status, detail = StageStatus.DEGRADED, "turn deadline too close to classify"
            else:
                try:
                    verdict = await self._classifier.classify(
                        masked_text, tenant=principal.tenant, timeout_s=bound_s
                    )
                except ClassifierError as exc:
                    status, detail = StageStatus.DEGRADED, str(exc)
                    downstream = DownstreamService.CLASSIFIER
                else:
                    status, detail = StageStatus.OK, verdict.summary()
                    _log.info(
                        "classifier.verdict",
                        sentiment=label_slug(verdict.sentiment.label),
                        intent=label_slug(verdict.intent.label) if verdict.intent else None,
                        urgency=label_slug(verdict.urgency.label),
                        requires_human=verdict.requires_human,
                        decided_by=label_slug(verdict.decided_by),
                    )
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        metrics.record_stage(stage=Stage.CLASSIFY, status=status, duration_s=latency_ms / 1000)
        yield StageEvent(
            stage=Stage.CLASSIFY,
            status=status,
            latency_ms=latency_ms,
            detail=detail,
            downstream=downstream,
        )

    def _should_redact(self, request: TurnRequest, principal: Principal) -> bool:
        """Customer channel: unconditional — `chat.py`'s `reject_privileged_options`
        already refuses a customer-channel caller who tries to set this. Business
        channel: the caller's own `options.redact_pii`, defaulting to on."""
        if principal.channel is Channel.CUSTOMER:
            return True
        profile = self._settings.guardrails.for_channel(principal.channel)
        if request.options.redact_pii is not None:
            return request.options.redact_pii
        return profile.redact_pii

    def _deadline_seconds(self, request: TurnRequest) -> float:
        """The effective per-turn deadline: the configured ceiling, narrowed
        (never widened) by the caller's own `options.deadline_ms` — the same
        rule every other `TurnOptions` field already follows."""
        ceiling_ms = self._settings.turn.deadline_ms
        requested_ms = request.options.deadline_ms
        effective_ms = ceiling_ms if requested_ms is None else min(requested_ms, ceiling_ms)
        return effective_ms / 1000

    # ── Pieces ────────────────────────────────────────────────────────────────

    async def _resolve_session(self, session_id: str | None, principal: Principal) -> Session:
        if session_id is not None:
            existing = await self._sessions.get(session_id, principal)
            if existing is not None:
                return existing
        return await self._sessions.create(principal)

    async def _resolve_confirmation(
        self, request: TurnRequest, session: Session
    ) -> PendingProposal | None:
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
            arguments={"note": "no air-action client exists yet — nothing executes"},
            risk="low",
            summary=(
                "A stand-in mutation, so the confirm path can be exercised before "
                "air-action exists."
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

    async def _answer(
        self,
        request: TurnRequest,
        masked_text: str,
        principal: Principal,
        session: Session,
        confirmed: PendingProposal | None,
        proposed: bool,
        outcome: _Synthesis,
        *,
        deadline_at: float | None,
    ) -> AsyncIterator[AnswerDeltaEvent]:
        """Synthesis proper. Fills ``outcome``; yields ``answer.delta`` events only
        when the caller is streaming (an async generator has no return value, hence
        the holder).

        Confirmation/proposal replies stay canned — there is no model call to make
        for a scripted stand-in mutation that touches nothing real, so the deadline
        and the prompt registry are irrelevant to those two branches.

        Raises `ModelGatewayError` on a real synthesis failure, and `TimeoutError`
        when the turn's deadline passes mid-stream — the caller decides the degraded
        fallback; this method's job is only to try.
        """
        if confirmed is not None:
            outcome.text = (
                f"Executed {confirmed.action} "
                "(no air-action client exists yet — nothing actually changed)."
            )
            return
        if proposed:
            outcome.text = (
                "That would change something, so here is a proposal rather than an action. "
                "Confirm it by sending the next turn with "
                '`"confirm": {"proposal_id": "…", "approve": true}`.'
            )
            return

        prompt = self._prompts.get("direct")
        messages = [{"role": "system", "content": prompt.system}]
        for turn in session.turns:
            messages.append({"role": turn.role, "content": turn.content})
        # Only the newest turn is demarcated, not replayed history: prior
        # assistant turns are this service's own output, and prior user turns
        # already went through this same boundary once when they were new.
        messages.append(
            {"role": "user", "content": delimit(masked_text, source="customer_message")}
        )
        outcome.prompt_version = f"{prompt.route}:{prompt.version}"

        if request.stream:
            outcome.streamed = True
            result: GenerateResult | None = None
            async for event in self._stream_prose(messages, deadline_at, outcome):
                if isinstance(event, AnswerDeltaEvent):
                    yield event
                else:
                    result = event
            assert result is not None  # `_stream_prose` always ends with the summary
        else:
            result = await self._gateway.generate(
                messages=messages,
                max_tokens=_SYNTHESIS_MAX_TOKENS,
                timeout=_remaining(deadline_at),
            )
        outcome.usage = _usage(result)
        if result.refusal:
            # Billed, so the usage stands; the content (empty on a refusal, partial
            # and already-streamed on a streamed one) does not. No structured
            # follow-up: a second call on a refused question would spend money to be
            # refused again.
            outcome.text = _PROVIDER_REFUSAL
            outcome.refused = True
            return
        text = result.content or "I don't have an answer for that."
        outcome.text = text

        if principal.channel is not Channel.BUSINESS:
            return
        turn_ceiling = request.options.max_cost_usd or self._settings.turn.max_cost_usd
        if request.output_schema is None:
            # The business channel's contract is structured output, so it
            # gets a body shaped like one even without a caller-supplied
            # schema — no second model call needed to wrap the same answer.
            outcome.structured = {"answer": text}
        elif outcome.usage.cost_usd >= turn_ceiling:
            # The per-turn ceiling is already met by the first call — cost
            # can only be known after a call completes, so enforcement is
            # of the *next* one: skip it and degrade to the free fallback
            # rather than spending further, uncontrolled.
            outcome.structured = {"answer": text}
            _log.info(
                "turn.cost_ceiling_reached",
                session_id=session.session_id,
                cost_usd=outcome.usage.cost_usd,
                ceiling_usd=turn_ceiling,
            )
        else:
            remaining = _remaining(deadline_at)
            if remaining is not None and remaining <= 0:
                return  # the prose answer spent the deadline; `structured` stays null
            structured, schema_usage = await self._structured_answer(
                masked_text, request.output_schema, timeout_s=remaining
            )
            outcome.structured = structured
            usage = outcome.usage
            usage.model_calls += schema_usage.model_calls
            usage.prompt_tokens += schema_usage.prompt_tokens
            usage.completion_tokens += schema_usage.completion_tokens
            usage.total_tokens += schema_usage.total_tokens
            usage.cost_usd += schema_usage.cost_usd

    async def _stream_prose(
        self, messages: list[dict[str, str]], deadline_at: float | None, outcome: _Synthesis
    ) -> AsyncIterator[AnswerDeltaEvent | GenerateResult]:
        """The prose call, streamed (spec 0004): guardrailed deltas, then the
        gateway's summary with ``content`` set to the whole raw text.

        Every delta passes through :class:`StreamRedactor`, which only ever releases
        a prefix of the final redacted answer — PII split across model chunks never
        streams (REQ-STRM-003). On a refusal nothing more is released: the
        authoritative answer that follows replaces what was sent (REQ-STRM-004).

        The deadline is checked on every read rather than wrapped around the loop:
        this generator is resumed from a different task for each event
        (``api/sse.py``), so a single ``asyncio.timeout`` would cancel the wrong one.
        Leaving the ``async with`` — normally, on the deadline, or because the client
        disconnected and the generator was closed — closes the gateway connection.
        """
        redactor = StreamRedactor()
        async with self._gateway.stream(
            messages=messages, max_tokens=_SYNTHESIS_MAX_TOKENS
        ) as stream:
            while True:
                remaining = _remaining(deadline_at)
                if remaining is not None and remaining <= 0:
                    raise TimeoutError
                item = await stream.next(timeout=remaining)
                if isinstance(item, StreamDelta):
                    safe = redactor.feed(item.text)
                    if safe:
                        yield AnswerDeltaEvent(text=safe)
                    continue
                result = item.result
                break
        if not result.refusal:
            tail = redactor.finish()
            if tail:
                yield AnswerDeltaEvent(text=tail)
        yield result.model_copy(update={"content": redactor.text})

    async def _structured_answer(
        self, masked_text: str, output_schema: dict[str, Any], *, timeout_s: float | None
    ) -> tuple[dict[str, Any] | None, Usage]:
        """A second, schema-constrained call for the business channel's
        `structured` field — the gateway is asked to conform via `json_schema`
        rather than validated after the fact and rejected, matching
        docs/02-lld.md's framing of schema enforcement as an output-guardrail
        concern, not a post-hoc filter.

        The gateway forwards the schema but does not check the answer, and says
        to treat a cut-off (`finish_reason: length`) or refused answer as no
        answer: truncated JSON can still parse into a plausible, wrong object."""
        try:
            result = await self._gateway.generate(
                messages=[
                    {
                        "role": "system",
                        "content": "Answer as JSON matching the given schema exactly.",
                    },
                    {
                        "role": "user",
                        "content": delimit(masked_text, source="customer_message"),
                    },
                ],
                max_tokens=_SYNTHESIS_MAX_TOKENS,
                json_schema=output_schema,
                schema_name="business_query",
                timeout=timeout_s,
            )
        except ModelGatewayError:
            # A failure here degrades this one field, not the whole turn —
            # the prose answer already succeeded, so the turn still answers.
            return None, Usage(model_calls=0, cost_usd=0.0, cache_hit=False)

        usage = _usage(result)
        if result.refusal or result.cut_off:
            return None, usage
        try:
            structured = json.loads(result.content) if result.content else None
        except json.JSONDecodeError:
            structured = None
        return structured if isinstance(structured, dict) else None, usage


class _Synthesis:
    """What synthesis produced — the holder `_answer` fills, since an async
    generator cannot return a value."""

    __slots__ = ("prompt_version", "refused", "streamed", "structured", "text", "usage")

    def __init__(self) -> None:
        self.text = ""
        self.structured: dict[str, Any] | None = None
        self.usage = Usage(model_calls=0, cost_usd=0.0, cache_hit=False)
        self.prompt_version: str | None = None
        self.refused = False
        self.streamed = False


def _remaining(deadline_at: float | None) -> float | None:
    """Seconds left before ``deadline_at`` (a ``perf_counter`` instant), or ``None``
    for an unbounded call."""
    return None if deadline_at is None else deadline_at - time.perf_counter()


def _usage(result: GenerateResult) -> Usage:
    """The gateway's wire counters, as this service's ``Usage`` for one call."""
    return Usage(
        model_calls=1,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        total_tokens=result.usage.prompt_tokens + result.usage.completion_tokens,
        cost_usd=result.cost_usd,
        cache_hit=result.cached or result.usage.cache_read_tokens > 0,
    )


async def collect(events: AsyncIterator[Event], *, include_trace: bool) -> TurnResult:
    """Fold an event stream into the non-streaming envelope. The streaming and
    JSON surfaces cannot drift, because there is only one engine and this is the
    only adapter over it."""
    result = TurnResult(turn_id="", session_id="", answer="")
    trace: list[StageRecord] = []
    degraded: list[str] = []

    async for event in events:
        match event.event:
            case EventType.TURN_START:
                result.turn_id = event.turn_id
                result.session_id = event.session_id
            case EventType.STAGE:
                # Only a stage degraded *by a downstream* names one: `degraded` is
                # "what was missing", and an exhausted deadline is not a service.
                if (
                    event.status is StageStatus.DEGRADED
                    and event.downstream is not None
                    and event.downstream.value not in degraded
                ):
                    degraded.append(event.downstream.value)
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
                result.escalated = event.escalated
                result.escalation_ref = event.escalation_ref
            case EventType.USAGE:
                result.usage = event.usage
            case EventType.ERROR:
                result.answer = result.answer or event.detail
            case EventType.TURN_END:
                result.status = event.status

    result.degraded = degraded
    if include_trace:
        result.trace = trace
    return result
