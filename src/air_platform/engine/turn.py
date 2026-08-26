"""The Phase 2a engine: guardrails, prompt registry and synthesis made real.

Replaces `engine/echo.py` (deleted alongside this). Same event contract, same
stage order, same session/proposal mechanics — `EchoEngine`'s own docstring
said this was the whole point of shipping the stubbed version first. What
changes is `GUARDRAILS_IN` (real injection screen + PII masking),
`SYNTHESISE` (a real air-llm call through the prompt registry) and
`GUARDRAILS_OUT` (real output PII/secret scan).

Still stubbed, deliberately, per this session's Phase 2a scoping — see
air-platform's `docs/00-plan.md`: `CACHE` (Phase 5), `CLASSIFY` and `GATHER`
(Phase 3, once air-classifier/air-rag/air-tools have clients here). Routing
stays the existing `/propose`-trigger stand-in for the same reason — with
`GATHER` still stubbed, there is nothing real to route to besides a direct
answer.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Final

import httpx

from air_platform.clients.llm import LlmClient
from air_platform.config import Settings
from air_platform.constants import (
    Channel,
    EventType,
    Route,
    Stage,
    StageStatus,
    TurnStatus,
)
from air_platform.guardrails.injection import screen_input
from air_platform.guardrails.pii import mask_input, scan_output
from air_platform.memory.session import InMemorySessionStore
from air_platform.observability import metrics
from air_platform.observability.logging import get_logger, safe_text_fields
from air_platform.prompts.registry import PromptRegistry
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

__all__ = ["TurnEngine", "TurnRequest", "collect"]

_log = get_logger(__name__)

_TURN_ID_PREFIX: Final[str] = "turn_"
_PROPOSAL_ID_PREFIX: Final[str] = "prop_"

#: Saying this in a message asks the engine to propose a mutation, so the
#: propose -> confirm -> execute path can be exercised end to end before
#: air-action exists (Phase 4). Unchanged from the echo engine — see the
#: module docstring on why routing itself stays a stand-in this pass.
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
#: diagram (air-platform/docs/01-hld.md's "LLM Inference Service" note).
_SYNTHESIS_MODEL: Final[str] = "generative"

_SYNTHESIS_MAX_TOKENS: Final[int] = 800


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
    signature, the same way this class replaced `EchoEngine` without changing
    it."""

    def __init__(self, settings: Settings, sessions: InMemorySessionStore, llm: LlmClient) -> None:
        self._settings = settings
        self._sessions = sessions
        self._llm = llm
        self._prompts = PromptRegistry(settings)

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
            async for event in self._pipeline(turn_id, request, principal, session):
                if isinstance(event, TurnEndEvent):  # pragma: no cover — defensive
                    continue
                if event.event is EventType.ANSWER and event.refusal:
                    status = TurnStatus.REFUSED
                yield event
        except Exception as exc:
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
        redact = self._should_redact(request, principal)
        mask = mask_input(request.text) if redact else None
        masked_text = mask.masked_text if mask is not None else request.text

        verdict = screen_input(masked_text)
        blocked_start = time.perf_counter()
        if verdict.blocked:
            metrics.record_guardrail_block(
                direction="in", rule=verdict.category or "unknown", channel=principal.channel
            )
        yield StageEvent(
            stage=Stage.GUARDRAILS_IN,
            status=StageStatus.BLOCKED if verdict.blocked else StageStatus.OK,
            latency_ms=round((time.perf_counter() - blocked_start) * 1000, 3),
            detail=f"prompt_injection: {verdict.matched_text!r}" if verdict.blocked else None,
        )

        if verdict.blocked:
            blocked = self._blocked_turn(turn_id, request, masked_text, principal, session)
            async for event in blocked:
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

        synth_started = time.perf_counter()
        text, structured, usage = await self._answer(
            request, masked_text, principal, session, confirmed, propose
        )
        yield StageEvent(
            stage=Stage.SYNTHESISE,
            status=StageStatus.OK,
            latency_ms=round((time.perf_counter() - synth_started) * 1000, 3),
        )

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

        yield AnswerEvent(text=text, structured=structured, grounded=False, refusal=False)

        started = time.perf_counter()
        window = self._settings.turn.window_turns
        session.append(Turn(turn_id=turn_id, role="user", content=masked_text), window=window)
        session.append(Turn(turn_id=turn_id, role="assistant", content=text), window=window)
        await self._sessions.save(session)
        yield StageEvent(
            stage=Stage.PERSIST,
            status=StageStatus.OK,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )

        yield UsageEvent(usage=usage)

    async def _blocked_turn(
        self,
        turn_id: str,
        request: TurnRequest,
        masked_text: str,
        principal: Principal,
        session: Session,
    ) -> AsyncIterator[Event]:
        """The guardrail-blocked path. Every later stage still emits — a client
        relies on the fixed stage sequence (docs/02-lld.md §4) — but reports
        why it did not run, and the turn ends with a refusal rather than a 5xx
        (docs/01-hld.md §4)."""
        skipped: Final = "turn blocked upstream"
        for stage in (Stage.CONTEXT, Stage.CACHE, Stage.CLASSIFY, Stage.PLAN, Stage.GATHER):
            yield StageEvent(
                stage=stage, status=StageStatus.SKIPPED, latency_ms=0.0, detail=skipped
            )
        yield RouteEvent(routes=[], reason="blocked before routing", capabilities=[])
        text = (
            "I can't help with that request — it looked like an attempt to change "
            "how I'm instructed to behave, rather than a question. Rephrase it and "
            "I'll try again."
        )
        yield StageEvent(
            stage=Stage.SYNTHESISE, status=StageStatus.SKIPPED, latency_ms=0.0, detail=skipped
        )
        yield StageEvent(stage=Stage.GUARDRAILS_OUT, status=StageStatus.OK, latency_ms=0.0)
        yield AnswerEvent(text=text, structured=None, grounded=False, refusal=True)

        started = time.perf_counter()
        window = self._settings.turn.window_turns
        session.append(Turn(turn_id=turn_id, role="user", content=masked_text), window=window)
        session.append(Turn(turn_id=turn_id, role="assistant", content=text), window=window)
        await self._sessions.save(session)
        yield StageEvent(
            stage=Stage.PERSIST,
            status=StageStatus.OK,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        yield UsageEvent(usage=Usage(model_calls=0, cost_usd=0.0, cache_hit=False))

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
    ) -> tuple[str, dict[str, Any] | None, Usage]:
        """The real thing `EchoEngine._answer` stubbed. Confirmation/proposal
        replies stay canned — there is no model call to make for a scripted
        stand-in mutation that touches nothing real."""
        if confirmed is not None:
            text = (
                f"Executed {confirmed.action} "
                "(no air-action client exists yet — nothing actually changed)."
            )
            return text, None, Usage(model_calls=0, cost_usd=0.0, cache_hit=False)
        if proposed:
            text = (
                "That would change something, so here is a proposal rather than an action. "
                "Confirm it by sending the next turn with "
                '`"confirm": {"proposal_id": "…", "approve": true}`.'
            )
            return text, None, Usage(model_calls=0, cost_usd=0.0, cache_hit=False)

        prompt = self._prompts.get("direct")
        messages = [{"role": "system", "content": prompt.system}]
        for turn in session.turns:
            messages.append({"role": turn.role, "content": turn.content})
        messages.append({"role": "user", "content": masked_text})

        result = await self._llm.chat(
            model=_SYNTHESIS_MODEL, messages=messages, max_tokens=_SYNTHESIS_MAX_TOKENS
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

        structured: dict[str, Any] | None = None
        if principal.channel is Channel.BUSINESS:
            if request.output_schema is None:
                # The business channel's contract is structured output, so it
                # gets a body shaped like one even without a caller-supplied
                # schema — no second model call needed to wrap the same answer.
                structured = {"answer": text}
            else:
                structured, schema_usage = await self._structured_answer(
                    masked_text, request.output_schema
                )
                usage.model_calls += schema_usage.model_calls
                usage.prompt_tokens += schema_usage.prompt_tokens
                usage.completion_tokens += schema_usage.completion_tokens
                usage.total_tokens += schema_usage.total_tokens
                usage.cost_usd += schema_usage.cost_usd

        return text, structured, usage

    async def _structured_answer(
        self, masked_text: str, output_schema: dict[str, Any]
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
                    {"role": "user", "content": masked_text},
                ],
                max_tokens=_SYNTHESIS_MAX_TOKENS,
                json_schema=output_schema,
                schema_name="business_query",
            )
        except httpx.HTTPError:
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
    """Fold an event stream into the non-streaming envelope. Unchanged from
    the echo engine — the two surfaces cannot drift because there is only one
    engine, whichever one is wired into the routes."""
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
                result.answer = result.answer or event.detail
            case EventType.TURN_END:
                result.status = event.status

    if include_trace:
        result.trace = trace
    return result
