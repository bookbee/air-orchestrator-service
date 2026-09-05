"""The turn engine: the pipeline in docs/01-hld.md §4, as an async generator.

``TurnEngine.run()`` yields :mod:`~air_orchestrator_service.schemas.events`
models in pipeline order. Streaming is the native shape and the non-streaming
route is the adapter (:func:`collect`), rather than the reverse — a turn that
fans out to several services takes seconds, and the client must be able to
render progress before the answer exists.

**What this engine actually does today.** Guardrails (injection screening, a
scope guard, PII masking in both directions, an escalation path), the prompt
registry, and synthesis through air-llm are real. So are the ceilings: a
turn-level deadline computed once and decremented before the synthesis call —
bound to that call's own timeout, and checked *before* a call is made so an
exhausted budget skips the call rather than starting it — plus per-turn and
per-session cost ceilings.

**What is stubbed, and why it says so.** ``CACHE`` (Phase 5) and ``CLASSIFY`` /
``GATHER`` (Phase 3, pending clients for air-classifier/air-rag/air-tools) report
``StageStatus.SKIPPED`` with a reason, never ``ok`` — see ``_STUBBED``. A client
must always be able to tell a stubbed turn from a real one, and this engine must
never be mistaken for a complete one in a demo. Routing is still the literal
``/propose`` trigger for the same reason: with ``GATHER`` stubbed there is
nothing to route to besides a direct answer, and a stand-in must not appear to
understand intent it does not.

**Status vocabulary is load-bearing.** ``TurnStatus.DEGRADED`` covers a provider
failure — air-llm being slow or down is the system coping, and the turn still
answers. ``TurnStatus.ERROR`` is reserved for genuinely unexpected exceptions.
Collapsing the two would make an outage and a bug indistinguishable on a
dashboard.

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

from air_orchestrator_service.clients.llm import LlmCallError, LlmClient
from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import (
    Channel,
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
from air_orchestrator_service.guardrails.pii import mask_input, scan_output
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
    Stage.CLASSIFY: "air-classifier has no client here yet (Phase 3)",
    Stage.GATHER: "no downstream services called yet (Phase 3)",
}

#: air-llm routing alias for synthesis — a role-scoped alias
#: (`routing.yaml`), not a literal model name, matching the per-role LLM
#: endpoint pattern this session's design review adopted from the reference
#: diagram (air-orchestrator-service/docs/01-hld.md's "LLM Inference Service" note).
_SYNTHESIS_MODEL: Final[str] = "generative"

_SYNTHESIS_MAX_TOKENS: Final[int] = 800

_INJECTION_REFUSAL: Final[str] = (
    "I can't help with that request — it looked like an attempt to change "
    "how I'm instructed to behave, rather than a question. Rephrase it and "
    "I'll try again."
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


class TurnEngine:
    """Runs a turn as an async generator of events. Phase 3 replaces `_gather`
    (currently absent — GATHER is still stubbed) without changing this
    signature."""

    def __init__(self, settings: Settings, sessions: InMemorySessionStore, llm: LlmClient) -> None:
        self._settings = settings
        self._sessions = sessions
        self._llm = llm
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
        async for event in self._stage(Stage.CLASSIFY):
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
        elapsed_s = time.perf_counter() - turn_started
        remaining_s = deadline_s - elapsed_s

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
        try:
            text, structured, usage, prompt_version = await self._answer(
                request,
                masked_text,
                principal,
                session,
                confirmed,
                propose,
                timeout_s=remaining_s if needs_model else None,
            )
        except LlmCallError as exc:
            self._last_status = TurnStatus.DEGRADED
            yield StageEvent(
                stage=Stage.SYNTHESISE,
                status=StageStatus.DEGRADED,
                latency_ms=round((time.perf_counter() - synth_started) * 1000, 3),
                detail=f"air-llm unavailable (retryable={exc.retryable}): {exc}",
            )
            async for event in self._finish_with_answer(
                turn_id, masked_text, principal, session, text=_DEGRADED_ANSWER
            ):
                yield event
            return

        yield StageEvent(
            stage=Stage.SYNTHESISE,
            status=StageStatus.OK,
            latency_ms=round((time.perf_counter() - synth_started) * 1000, 3),
            detail=f"prompt={prompt_version}" if prompt_version else None,
        )
        async for event in self._finish_with_answer(
            turn_id, masked_text, principal, session, text=text, structured=structured, usage=usage
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
        *,
        timeout_s: float | None,
    ) -> tuple[str, dict[str, Any] | None, Usage, str | None]:
        """Synthesis proper. Confirmation/proposal replies stay canned —
        there is no model call to make for a scripted
        stand-in mutation that touches nothing real, so `timeout_s` and the
        prompt registry are irrelevant to those two branches.

        Raises `LlmCallError` on a real synthesis failure — the caller
        decides the degraded fallback; this method's job is only to try.
        """
        if confirmed is not None:
            text = (
                f"Executed {confirmed.action} "
                "(no air-action client exists yet — nothing actually changed)."
            )
            return text, None, Usage(model_calls=0, cost_usd=0.0, cache_hit=False), None
        if proposed:
            text = (
                "That would change something, so here is a proposal rather than an action. "
                "Confirm it by sending the next turn with "
                '`"confirm": {"proposal_id": "…", "approve": true}`.'
            )
            return text, None, Usage(model_calls=0, cost_usd=0.0, cache_hit=False), None

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

        result = await self._llm.chat(
            model=_SYNTHESIS_MODEL,
            messages=messages,
            max_tokens=_SYNTHESIS_MAX_TOKENS,
            timeout=timeout_s,
        )
        text = result.content or "I don't have an answer for that."
        usage = Usage(
            model_calls=1,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            total_tokens=result.usage.prompt_tokens + result.usage.completion_tokens,
            cost_usd=result.cost_usd,
            cache_hit=result.usage.cache_read_tokens > 0,
        )
        prompt_version = f"{prompt.route}:{prompt.version}"

        structured: dict[str, Any] | None = None
        if principal.channel is Channel.BUSINESS:
            turn_ceiling = request.options.max_cost_usd or self._settings.turn.max_cost_usd
            if request.output_schema is None:
                # The business channel's contract is structured output, so it
                # gets a body shaped like one even without a caller-supplied
                # schema — no second model call needed to wrap the same answer.
                structured = {"answer": text}
            elif usage.cost_usd >= turn_ceiling:
                # The per-turn ceiling is already met by the first call — cost
                # can only be known after a call completes, so enforcement is
                # of the *next* one: skip it and degrade to the free fallback
                # rather than spending further, uncontrolled.
                structured = {"answer": text}
                _log.info(
                    "turn.cost_ceiling_reached",
                    session_id=session.session_id,
                    cost_usd=usage.cost_usd,
                    ceiling_usd=turn_ceiling,
                )
            else:
                structured, schema_usage = await self._structured_answer(
                    masked_text, request.output_schema, timeout_s=timeout_s
                )
                usage.model_calls += schema_usage.model_calls
                usage.prompt_tokens += schema_usage.prompt_tokens
                usage.completion_tokens += schema_usage.completion_tokens
                usage.total_tokens += schema_usage.total_tokens
                usage.cost_usd += schema_usage.cost_usd

        return text, structured, usage, prompt_version

    async def _structured_answer(
        self, masked_text: str, output_schema: dict[str, Any], *, timeout_s: float | None
    ) -> tuple[dict[str, Any] | None, Usage]:
        """A second, schema-constrained call for the business channel's
        `structured` field — air-llm is asked to conform via `json_schema`
        rather than validated after the fact and rejected, matching
        docs/02-lld.md's framing of schema enforcement as an output-guardrail
        concern, not a post-hoc filter."""
        try:
            result = await self._llm.chat(
                model=_SYNTHESIS_MODEL,
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
        except LlmCallError:
            # A failure here degrades this one field, not the whole turn —
            # the prose answer already succeeded, so the turn still answers.
            return None, Usage(model_calls=0, cost_usd=0.0, cache_hit=False)

        try:
            structured = json.loads(result.content) if result.content else None
        except json.JSONDecodeError:
            structured = None
        usage = Usage(
            model_calls=1,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            total_tokens=result.usage.prompt_tokens + result.usage.completion_tokens,
            cost_usd=result.cost_usd,
            cache_hit=result.usage.cache_read_tokens > 0,
        )
        return structured if isinstance(structured, dict) else None, usage


async def collect(events: AsyncIterator[Event], *, include_trace: bool) -> TurnResult:
    """Fold an event stream into the non-streaming envelope. The streaming and
    JSON surfaces cannot drift, because there is only one engine and this is the
    only adapter over it."""
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
                result.escalated = event.escalated
                result.escalation_ref = event.escalation_ref
            case EventType.USAGE:
                result.usage = event.usage
            case EventType.ERROR:
                result.answer = result.answer or event.detail
            case EventType.TURN_END:
                result.status = event.status

    if include_trace:
        result.trace = trace
    return result
