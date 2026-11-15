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
per-session cost ceilings. The turn ceiling is enforced the same way, before the
money is spent: each paid call is priced from configured rates first, and skipped
if it could take the turn past the ceiling (spec 0008). Cost is otherwise only
known once a call returns, which is too late for a cap.

**What is stubbed, and why it says so.** ``CACHE`` (Phase 5) reports
``StageStatus.SKIPPED`` with a reason, never ``ok`` — see ``_STUBBED``. A client
must always be able to tell a stubbed turn from a real one, and this engine must
never be mistaken for a complete one in a demo.

**Routing is a rule, behind a planner seam.** ``PLAN`` asks a ``Planner``
(``engine/plan.py``) for the routes and steps, and this module carries them out.
The default, ``RulePlanner``, is a fixed rule: retrieve on the customer channel
when air-rag-service serves the tenant, and treat the literal ``/propose`` as the
trigger for the proposal path. A stand-in must not appear to understand intent it
does not, so it plans no air-tools-gateway step; spec 0013's model planner will.
``GATHER`` is real for air-rag-service (spec 0011): its passages reach the prompt
numbered and delimited, and each one the answer cites by ``[n]`` becomes a
``citation``.

**A change is proposed on one turn and executed on another.** A planned
air-tools-gateway ``write`` is prepared by the gateway in ``GATHER`` and published as
a ``prop_`` proposal; nothing changes (spec 0012). Only a later turn whose structured
``confirm`` names that proposal, from the same key and customer, executes it, again
in ``GATHER``'s slot, through :class:`ActionExecutor` alone. The pending proposal is
consumed before anything is sent, so the execute happens at most once from here
(``REQ-TGW-018``). Both turns answer with fixed text: the execute outcome picks one
of five, and only ``done``'s says the change was made (``REQ-TGW-017``).

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

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import replace
from typing import Any, Final

import orjson

from air_orchestrator_service.api.errors import SessionNotFoundError
from air_orchestrator_service.clients.classifier import (
    MIN_CALL_MS,
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
from air_orchestrator_service.clients.rag import MIN_CALL_MS as RAG_MIN_CALL_MS
from air_orchestrator_service.clients.rag import Passage, RagClient, RagError, safe_title, safe_uri
from air_orchestrator_service.clients.tools_gateway import (
    EXECUTE_NOT_DONE_CODES,
    ActionExecutor,
    Capability,
    GatewayProposal,
    Inventory,
    ToolResult,
    ToolsGatewayClient,
    ToolsGatewayError,
    is_capability_name,
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
from air_orchestrator_service.engine.cost import estimate_cost
from air_orchestrator_service.engine.plan import (
    FollowUp,
    Plan,
    PlanContext,
    Planner,
    RulePlanner,
    Step,
)
from air_orchestrator_service.guardrails.boundary import delimit
from air_orchestrator_service.guardrails.escalation import wants_human
from air_orchestrator_service.guardrails.injection import screen_input
from air_orchestrator_service.guardrails.pii import StreamRedactor, mask_input, scan_output
from air_orchestrator_service.guardrails.scope import screen_scope
from air_orchestrator_service.memory.session import InMemorySessionStore
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.observability.logging import get_logger, safe_text_fields
from air_orchestrator_service.prompts.registry import Prompt, PromptRegistry, prompt_for
from air_orchestrator_service.schemas.chat import (
    ChatRequest,
    Citation,
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
    CitationEvent,
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

#: Stages this engine still cannot really run. `SYNTHESISE` is not here any
#: more — see the module docstring.
_STUBBED: Final[dict[Stage, str]] = {
    Stage.CACHE: "semantic cache is Phase 5",
}

#: A citation marker in the answer: ``[1]`` .. ``[99]`` (spec 0011, ``REQ-RAG-009``).
_CITATION_MARKER: Final = re.compile(r"\[(\d{1,2})\]")
#: How much of a cited passage the caller sees. The model saw the whole snippet.
_CITATION_SNIPPET_CHARS: Final[int] = 300
_CONTROL_CHARS: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_SOURCES_HEADER: Final[str] = (
    "Sources from the knowledge base for this message, numbered. Answer from them, and "
    "cite each one you use as [n]."
)
#: Spec 0011, REQ-RAG-011 and HLD §7: the answer says it could not check, rather than
#: answering as if it had.
_KB_UNAVAILABLE: Final[str] = (
    "The knowledge base could not be consulted for this message. If the answer depends on "
    "policies, terms or product details, say briefly that you could not check them just "
    "now, and do not state them."
)

#: Spec 0012, REQ-TGW-009/011: lookups reach the prompt as data, and a lookup that
#: could not be made is named so the answer says so instead of guessing.
#: The rules for using lookups travel with them, not in the system prompt
#: (``REQ-TGW-027``): a turn without lookups keeps the reviewed channel prompt, which
#: says plainly it cannot look anything up, and this message is the one exception.
_LOOKUPS_HEADER: Final[str] = (
    "Results of lookups made for this message in the business systems, one block per "
    "lookup. Answer from them: state order numbers, statuses, dates and amounts exactly as "
    "they appear, and never state a detail they do not contain or fill a gap with a typical "
    "value. You may say you checked. If a block is marked partial, say you are showing only "
    "some of the records. Do not mention lookups, results or blocks to the customer."
)
_LOOKUPS_FAILED: Final[str] = (
    "These lookups could not be made for this message: {names}. If the answer depends on "
    "them, say briefly that you could not check just now, and do not guess. Offer to try "
    "again in a little while, or tell the customer they can ask to speak to a person."
)
#: Spec 0013 §7: round 2 of planning failed, so a change the customer asked for was
#: not prepared. The answer says so rather than implying it was.
_CHANGE_NOT_PREPARED: Final[str] = (
    "A change the customer may have asked for could not be prepared just now. If they asked "
    "for one, say briefly that you could not prepare it, that nothing has been changed, and "
    "that they can try again in a little while."
)
#: How a failed lookup reads in `GATHER`'s detail, by spec 0010's reason word.
_LOOKUP_FAILURE: Final[dict[str, str]] = {
    "timeout": "timed out",
    "unavailable": "unreachable",
    "error": "failed",
    "breaker_open": "circuit open",
}
#: Spec 0012 §7: what a proposing turn says. Fixed text, never synthesised: a turn that
#: proposes a change calls no model, and the gateway's ``summary`` is the only
#: downstream text in it (cleaned, and capped at 500 characters by the contract).
_PROPOSAL_READY: Final[str] = (
    "{summary}\n\nNothing has been changed yet. If you would like to go ahead, please confirm."
)
_PROPOSE_FAILED: Final[str] = (
    "I couldn't prepare that change just now. Please try again in a little while, or ask "
    "to speak to a person."
)
#: ``REQ-TGW-028``: a refusal is answered from its code, by fixed text.
_PROPOSE_REFUSED: Final[dict[str, str]] = {
    "not_found": (
        "I couldn't find that on your account, so I can't prepare that change. Could you "
        "check the order number?"
    ),
    "invalid_state": (
        "That change isn't possible in the order's current state, for example once it has "
        "shipped. If you need more help, you can ask to speak to a person."
    ),
    "not_permitted": (
        "That change can't be made here. If you need it, you can ask to speak to a person."
    ),
    "limit_exceeded": (
        "That change can't be made right now because a limit has been reached. You can ask "
        "to speak to a person for help."
    ),
}
#: Spec 0012 ``REQ-TGW-017``: what a confirmed turn says, by the execute outcome. Only
#: ``done``'s text says the change was made. None names the change, so none can say
#: "cancelled" when it was not.
_EXECUTE_DONE: Final[str] = "Done: the change you confirmed has been made."
_EXECUTE_PENDING: Final[str] = (
    "That change needs an approval first, so it has not been made yet. You can ask me "
    "again later to check whether it has gone through."
)
#: ``rejected``, worded from the gateway's closed-set ``code``, never its free text.
_EXECUTE_REJECTED: Final[dict[str, str]] = {
    "invalid_state": (
        "That change could not be made in the order's current state, for example once it "
        "has shipped, so nothing has been changed."
    ),
    "not_found": "I couldn't find that on your account, so nothing has been changed.",
    "not_permitted": (
        "That change can't be made here, so nothing has been changed. You can ask to "
        "speak to a person."
    ),
    "limit_exceeded": (
        "That change could not be made because a limit has been reached, so nothing has "
        "been changed. You can ask to speak to a person for help."
    ),
}
_EXECUTE_REJECTED_OTHER: Final[str] = (
    "That change could not be made, so nothing has been changed. You can ask to speak to "
    "a person for help."
)
#: A request was sent and no outcome came back: it may or may not have happened.
_EXECUTE_UNKNOWN: Final[str] = (
    "I couldn't confirm whether that change went through. Please don't ask for it again "
    "yet: ask me to check on it in a little while, or ask to speak to a person."
)
#: No request was sent (an open circuit, no time left, no gateway).
_EXECUTE_NOT_DONE: Final[str] = (
    "I couldn't make that change just now, so nothing has been changed. Please try again "
    "in a little while, or ask to speak to a person."
)
#: The gateway answered that it acted on nothing: it does not know the proposal, or it
#: expired (``REQ-TGW-017``). Confirming again cannot help; asking afresh can.
_EXECUTE_NOT_DONE_STALE: Final[str] = (
    "That change wasn't made, because it could no longer be carried out as confirmed, so "
    "nothing has been changed. If you still want it, ask me again and I'll prepare it "
    "afresh."
)
#: The shortest bound worth a lookup; below it the turn reports the deadline instead.
_LOOKUP_MIN_MS: Final[int] = 100

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
_COST_CEILING_ANSWER: Final[str] = (
    "I can't put together an answer within this request's cost limit. Try again "
    "with a higher limit — nothing about your account or order has changed."
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
    """Runs a turn as an async generator of events."""

    def __init__(
        self,
        settings: Settings,
        sessions: InMemorySessionStore,
        model_gateway: ModelGatewayClient,
        classifier: ClassifierClient | None = None,
        rag: RagClient | None = None,
        planner: Planner | None = None,
        tools_gateway: ToolsGatewayClient | None = None,
        action_executor: ActionExecutor | None = None,
    ) -> None:
        self._settings = settings
        #: Decides the routes and steps at ``PLAN`` (spec 0012 T5). Today's rule
        #: unless one is given.
        self._planner: Planner = planner or RulePlanner()
        self._sessions = sessions
        self._gateway = model_gateway
        #: ``None`` when air-classifier is not configured (spec 0005).
        self._classifier = classifier
        #: ``None`` when air-rag-service is not configured (spec 0011).
        self._rag = rag
        #: What `GATHER` found for this turn: the passages the prompt carries, and
        #: whether air-rag-service could not be consulted. Per request, like `_last_status`.
        self._sources: list[Passage] = []
        self._kb_unavailable = False
        self._retrieval_cost_usd = 0.0
        #: ``None`` when air-tools-gateway is not configured (spec 0012).
        self._tools_gateway = tools_gateway
        #: What `GATHER` looked up for this turn, as ``(capability, text, partial)``,
        #: and what it could not. Per request, like `_sources`.
        self._lookups: list[tuple[str, str, bool]] = []
        self._lookup_notices: list[str] = []
        #: What `GATHER`'s propose call produced, and the fixed answer for the
        #: turn when it planned a change (spec 0012). Per request.
        self._gateway_proposal: tuple[Capability, Step, GatewayProposal] | None = None
        self._proposal_answer: str | None = None
        #: The execute-key client, reached only from a confirmation turn (spec 0012
        #: plan §2), and the fixed answer its outcome chose. Per request.
        self._executor = action_executor
        self._execution_answer: str | None = None
        self._prompts = PromptRegistry(settings)
        #: Set by `_pipeline` when it degrades a turn — an async generator has
        #: no return value, so this is how `run()` learns the outcome once
        #: iteration finishes, the same way it already reads `refusal` off
        #: the `AnswerEvent` for `TurnStatus.REFUSED`. One `TurnEngine` is
        #: built fresh per request (`api/v1/chat.py`), so there is no
        #: cross-request state here to worry about.
        self._last_status: TurnStatus = TurnStatus.OK
        #: What air-classifier reported spending on this turn (spec 0008,
        #: ``REQ-COST-005``). Per request, like `_last_status`.
        self._classifier_cost_usd = 0.0
        #: air-classifier's slug-checked labels, shown to the planner (spec 0013,
        #: ``REQ-PLN-002``). Per request.
        self._intent: str | None = None
        self._sentiment: str | None = None
        #: What planning spent, both rounds (spec 0013, ``REQ-PLN-005``). Per request.
        self._planning_usage = Usage(model_calls=0, cost_usd=0.0, cache_hit=False)
        #: Round 2 could not run, so the answer says the change was not prepared.
        self._change_not_prepared = False
        #: Set by `open()`: the turn's clock and its session.
        self._opened: tuple[float, Session] | None = None

    async def open(self, request: TurnRequest, principal: Principal) -> None:
        """Start the turn's clock and resolve its session, before any event.

        Raises :class:`SessionNotFoundError` for a ``session_id`` this caller cannot
        see (spec 0009, ``REQ-VAL-001``). A route awaits this *before* it picks a
        response shape, so on a streaming request the 404 is the status line, not an
        ``error`` event after headers have gone. :meth:`run` calls it when a caller
        did not.
        """
        started = time.perf_counter()
        session = await self._resolve_session(request.session_id, principal)
        self._opened = (started, session)

    async def run(self, request: TurnRequest, principal: Principal) -> AsyncIterator[Event]:
        turn_id = f"{_TURN_ID_PREFIX}{uuid.uuid4().hex}"
        status = TurnStatus.OK

        if self._opened is None:
            await self.open(request, principal)
        assert self._opened is not None
        started, session = self._opened

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
        confirmed = await self._resolve_confirmation(request, session, principal)

        async for event in self._stage(Stage.CONTEXT):
            yield event
        async for event in self._stage(Stage.CACHE):
            yield event
        async for event in self._classify(
            request,
            masked_text,
            principal,
            session,
            deadline_at=turn_started + deadline_s,
            deadline_s=deadline_s,
        ):
            yield event

        plan_started = time.perf_counter()
        offered = await self._turn_inventory(
            principal,
            confirming=confirmed is not None,
            deadline_at=turn_started + deadline_s,
            deadline_s=deadline_s,
        )
        plan_ctx = PlanContext(
            text=masked_text,
            principal=principal,
            confirming=confirmed is not None,
            rag_serves=self._rag is not None and self._rag.settings.serves(principal.tenant),
            offered=offered,
            history=tuple((turn.role, turn.content) for turn in session.turns),
            intent=self._intent,
            sentiment=self._sentiment,
            deadline_s=deadline_s,
            deadline_at=turn_started + deadline_s,
            max_cost_usd=self._planning_allowance(request, principal, session, masked_text),
        )
        planned = await self._planner.plan(plan_ctx)
        self._add_planning(planned.usage)
        if planned.status is StageStatus.DEGRADED:
            self._last_status = TurnStatus.DEGRADED
        plan_ms = round((time.perf_counter() - plan_started) * 1000, 3)
        metrics.record_stage(stage=Stage.PLAN, status=planned.status, duration_s=plan_ms / 1000)
        yield StageEvent(
            stage=Stage.PLAN,
            status=planned.status,
            latency_ms=plan_ms,
            detail=planned.detail,
            downstream=planned.downstream,
        )
        plan = planned.plan
        if planned.prompt_ref is not None:
            # REQ-PLN-009: the planner prompt's version, with every planned turn.
            _log.info("turn.planned", planner_prompt=planned.prompt_ref, status=planned.status)
        if plan.write is not None and (
            not principal.allow_actions or principal.channel is Channel.BUSINESS
        ):
            # REQ-TGW-020/026: dropped before the route event, silently, so the turn
            # reads as one that never planned a change (REQ-MUT-012, spec 0001 Q1).
            plan = _without_write(plan)
        write = plan.write if confirmed is None else None
        propose = plan.stand_in_proposal
        # A proposing turn's answer is fixed text, so it needs no model, no
        # retrieval and no lookups.
        needs_model = confirmed is None and not propose and write is None
        retrieve = needs_model and plan.retrieve
        yield RouteEvent(
            routes=list(plan.routes),
            reason=plan.reason,
            # Only what the plan uses, and only names the inventory holds: a name
            # the planner made up is never echoed (REQ-TGW-025).
            capabilities=[
                name
                for name in plan.capabilities
                if name == DownstreamService.RAG.value or offered.get(name) is not None
            ],
        )

        async for event in self._gather(
            masked_text,
            principal,
            retrieve=retrieve,
            needs_model=needs_model,
            reads=plan.reads if needs_model else (),
            follow_up=(plan_ctx, plan) if needs_model and plan.needs_results else None,
            write=write,
            offered=offered,
            execute=confirmed,
            deadline_at=turn_started + deadline_s,
            deadline_s=deadline_s,
        ):
            yield event

        if propose:
            yield ProposalEvent(proposal=await self._propose(session, principal))
        elif self._gateway_proposal is not None:
            yield ProposalEvent(proposal=await self._propose_gateway(session, principal))

        # ── Deadline check, right before the expensive part. A canned
        # confirm/propose reply never calls the model, so the deadline is
        # irrelevant to it — only a real synthesis call is bounded. ──
        remaining_s = deadline_s - (time.perf_counter() - turn_started)

        if needs_model and self._proposal_answer is None and remaining_s <= 0:
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
        except _CostCeilingError as exc:
            # Spec 0008, REQ-COST-002: the call was never made. Ours, not the
            # gateway's, so no `downstream` is named.
            self._last_status = TurnStatus.DEGRADED
            yield StageEvent(
                stage=Stage.SYNTHESISE,
                status=StageStatus.DEGRADED,
                latency_ms=round((time.perf_counter() - synth_started) * 1000, 3),
                detail=str(exc),
            )
            async for event in self._finish_with_answer(
                turn_id, masked_text, principal, session, text=_COST_CEILING_ANSWER
            ):
                yield event
            return
        except ModelGatewayError as exc:
            self._last_status = TurnStatus.DEGRADED
            if exc.timed_out and outcome.deadline_bound:
                # REQ-MGW-018: earlier stages left this call too little of the
                # turn, so the deadline ran out, not the gateway.
                yield StageEvent(
                    stage=Stage.SYNTHESISE,
                    status=StageStatus.DEGRADED,
                    latency_ms=round((time.perf_counter() - synth_started) * 1000, 3),
                    detail="turn deadline reached during the model call",
                )
                async for event in self._finish_with_answer(
                    turn_id, masked_text, principal, session, text=_DEGRADED_ANSWER
                ):
                    yield event
                return
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

        for citation in self._citations(text):
            yield CitationEvent(citation=citation)
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
        planning = self._planning_usage
        if planning.model_calls:
            # Spec 0013 §8: planning's calls, tokens and cost, attributed as planning.
            resolved_usage = resolved_usage.model_copy(
                update={
                    "model_calls": resolved_usage.model_calls + planning.model_calls,
                    "prompt_tokens": resolved_usage.prompt_tokens + planning.prompt_tokens,
                    "completion_tokens": resolved_usage.completion_tokens
                    + planning.completion_tokens,
                    "total_tokens": resolved_usage.total_tokens + planning.total_tokens,
                    "cost_usd": resolved_usage.cost_usd + planning.cost_usd,
                    "planning_cost_usd": planning.cost_usd,
                }
            )
        if self._classifier_cost_usd or self._retrieval_cost_usd:
            # Every path that ran `CLASSIFY` or `GATHER` passes through here, so their
            # spend is reported even on a turn that degraded afterwards (REQ-COST-005,
            # REQ-RAG-013).
            resolved_usage = resolved_usage.model_copy(
                update={
                    "cost_usd": resolved_usage.cost_usd
                    + self._classifier_cost_usd
                    + self._retrieval_cost_usd,
                    "classifier_cost_usd": self._classifier_cost_usd,
                    "retrieval_cost_usd": self._retrieval_cost_usd,
                }
            )
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
        self,
        request: TurnRequest,
        masked_text: str,
        principal: Principal,
        session: Session,
        *,
        deadline_at: float,
        deadline_s: float,
    ) -> AsyncIterator[Event]:
        """The ``CLASSIFY`` stage (spec 0005): one stage event, whatever happens.

        The verdict is observed, not yet acted on: it reaches the stage detail and
        the log, and nothing downstream reads it (``REQ-CLS-011``/``012`` are
        deferred). So every failure here is soft by construction. The stage says
        what went wrong and the turn's status is untouched (HLD §7, "Turn
        unaffected"). The customer channel only — see the spec's §3 on why a
        business query is not classified.

        Enrichment, so it yields to the answer twice over: it gets at most a share
        of the turn's deadline (``REQ-CLS-014``), and only the cost the ceiling
        leaves once the synthesis call that follows is reserved (``REQ-CLS-015``).
        """
        started = time.perf_counter()
        status, detail, downstream = StageStatus.SKIPPED, None, None
        if self._classifier is None:
            detail = "air-classifier is not configured"
        elif principal.channel is not Channel.CUSTOMER:
            detail = "not used on the business channel"
        else:
            bound_s = self._classifier.bound_s(_remaining(deadline_at), deadline_s=deadline_s)
            _, messages = self._prose_messages(principal, session, masked_text)
            affordable = self._turn_ceiling(request) - self._estimate(
                messages, _SYNTHESIS_MAX_TOKENS
            )
            if bound_s * 1000 < MIN_CALL_MS:
                # Ours, not the classifier's: no `downstream` is named.
                status, detail = StageStatus.DEGRADED, "turn deadline too close to classify"
            elif affordable <= 0:
                status, detail = StageStatus.DEGRADED, "cost ceiling: nothing left to classify"
            else:
                try:
                    verdict = await self._classifier.classify(
                        masked_text,
                        tenant=principal.tenant,
                        timeout_s=bound_s,
                        max_cost_usd=affordable,
                    )
                except ClassifierError as exc:
                    status, detail = StageStatus.DEGRADED, str(exc)
                    downstream = DownstreamService.CLASSIFIER
                else:
                    status, detail = StageStatus.OK, verdict.summary()
                    self._classifier_cost_usd = verdict.usage.est_cost_usd
                    self._sentiment = label_slug(verdict.sentiment.label)
                    self._intent = label_slug(verdict.intent.label) if verdict.intent else None
                    _log.info(
                        "classifier.verdict",
                        sentiment=label_slug(verdict.sentiment.label),
                        intent=label_slug(verdict.intent.label) if verdict.intent else None,
                        urgency=label_slug(verdict.urgency.label),
                        requires_human=verdict.requires_human,
                        decided_by=label_slug(verdict.decided_by),
                        cost_usd=verdict.usage.est_cost_usd,
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

    async def _gather(
        self,
        masked_text: str,
        principal: Principal,
        *,
        retrieve: bool,
        needs_model: bool,
        reads: tuple[Step, ...],
        write: Step | None,
        offered: Inventory,
        execute: PendingProposal | None,
        deadline_at: float,
        deadline_s: float,
        follow_up: tuple[PlanContext, Plan] | None = None,
    ) -> AsyncIterator[Event]:
        """The ``GATHER`` stage (specs 0011, 0012): one stage event, whatever happens.

        Retrieval and the plan's lookups run concurrently, each bounded by its own
        share of the turn's deadline (``REQ-RAG-004``, ``REQ-TGW-010``), and the one
        event reports both. A planned change is prepared here too, by the gateway's
        propose call, so its outcome is this stage's to report (a proposing turn
        retrieves and looks up nothing). A confirmed one is executed here as well, on
        a turn that does nothing else (plan §1, no new ``Stage``). With none of them, the event is
        retrieval's alone, exactly as before spec 0012. ``StageEvent.downstream`` is
        one field, so when several fail it names air-rag first and the detail names
        them all.

        A plan that asked to see its reads' results (spec 0013) gets one more planning
        round once they are back, and only then its proposal, so that chain runs as one
        part beside retrieval. Round 2 reports here, never as a second ``PLAN`` event,
        so stage events keep ``Stage`` order (``REQ-PLN-011``).
        """
        started = time.perf_counter()
        retrieval, (lookups, second, proposal), execution = await asyncio.gather(
            self._retrieval_part(
                masked_text,
                principal,
                retrieve=retrieve,
                needs_model=needs_model,
                deadline_at=deadline_at,
                deadline_s=deadline_s,
            ),
            self._tools_parts(
                principal,
                reads,
                write,
                offered,
                follow_up,
                deadline_at=deadline_at,
                deadline_s=deadline_s,
            ),
            self._execution_part(principal, execute, deadline_at=deadline_at),
        )
        status, detail, downstream = _combine(retrieval, lookups, second, proposal, execution)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        metrics.record_stage(stage=Stage.GATHER, status=status, duration_s=latency_ms / 1000)
        yield StageEvent(
            stage=Stage.GATHER,
            status=status,
            latency_ms=latency_ms,
            detail=detail,
            downstream=downstream,
        )

    async def _tools_parts(
        self,
        principal: Principal,
        reads: tuple[Step, ...],
        write: Step | None,
        offered: Inventory,
        follow_up: tuple[PlanContext, Plan] | None,
        *,
        deadline_at: float,
        deadline_s: float,
    ) -> tuple[_Part | None, _Part | None, _Part | None]:
        """Lookups, then round 2 when the plan asked for it, then the proposal: the
        parts of ``GATHER`` that run one after another."""
        lookups = await self._lookup_part(
            principal, reads, offered, deadline_at=deadline_at, deadline_s=deadline_s
        )
        second: _Part | None = None
        if follow_up is not None and reads:
            second, write = await self._second_round(follow_up)
        proposal = await self._proposal_part(
            principal, write, offered, deadline_at=deadline_at, deadline_s=deadline_s
        )
        return lookups, second, proposal

    async def _second_round(self, follow_up: tuple[PlanContext, Plan]) -> tuple[_Part, Step | None]:
        """Round 2 (spec 0013, ``REQ-PLN-007``): one call, after the reads, shown their
        results exactly as the answer prompt will be. It may propose, and nothing more.
        Its step goes through the same checks as a round-1 one (``REQ-PLN-008``)."""
        ctx, first = follow_up
        if not self._lookups:
            return (StageStatus.OK, "round 2: skipped: no lookup results", None), None
        remaining_cost = self._turn_ceiling_left(ctx)
        answer: FollowUp = await self._planner.follow_up(
            replace(ctx, max_cost_usd=remaining_cost), first, self._lookup_blocks()
        )
        self._add_planning(answer.usage)
        if answer.failed:
            self._last_status = TurnStatus.DEGRADED
            self._change_not_prepared = True
            return (StageStatus.DEGRADED, answer.detail, answer.downstream), None
        return (StageStatus.OK, answer.detail, None), answer.write

    def _add_planning(self, usage: Usage | None) -> None:
        if usage is None:
            return
        total = self._planning_usage
        total.model_calls += usage.model_calls
        total.prompt_tokens += usage.prompt_tokens
        total.completion_tokens += usage.completion_tokens
        total.total_tokens += usage.total_tokens
        total.cost_usd += usage.cost_usd
        total.planning_cost_usd += usage.cost_usd

    def _planning_allowance(
        self, request: TurnRequest, principal: Principal, session: Session, masked_text: str
    ) -> float:
        """What the turn ceiling leaves for planning once the answer is reserved, as
        ``CLASSIFY`` reserves it (``REQ-PLN-005``, ``REQ-CLS-015``)."""
        _, messages = self._prose_messages(principal, session, masked_text)
        return (
            self._turn_ceiling(request)
            - self._classifier_cost_usd
            - self._estimate(messages, _SYNTHESIS_MAX_TOKENS)
        )

    def _turn_ceiling_left(self, ctx: PlanContext) -> float:
        """Round 2's allowance: round 1's, less what round 1 spent."""
        return ctx.max_cost_usd - self._planning_usage.cost_usd

    async def _retrieval_part(
        self,
        masked_text: str,
        principal: Principal,
        *,
        retrieve: bool,
        needs_model: bool,
        deadline_at: float,
        deadline_s: float,
    ) -> _Part:
        """air-rag-service's half of ``GATHER`` (spec 0011).

        Retrieval is for grounding an answer, so it runs only on a customer turn that
        will be synthesised. It is bounded like classification: a share of the turn's
        deadline, never more than is left (``REQ-RAG-004``). A failure narrows the
        answer rather than refusing it. The prompt then says the knowledge base could
        not be consulted, and the turn is ``degraded`` naming ``air-rag``, because an
        answer given without the sources it should have had is a lesser answer
        (HLD §7, ``REQ-RAG-011``). That differs from ``CLASSIFY``, whose verdict
        nothing reads yet.
        """
        status, detail, downstream = StageStatus.SKIPPED, None, None
        if self._rag is None:
            detail = "air-rag is not configured"
        elif principal.channel is not Channel.CUSTOMER:
            detail = "not used on the business channel"
        elif not needs_model:
            detail = "nothing to ground: a proposal or confirmation turn"
        elif not self._rag.settings.serves(principal.tenant):
            # REQ-RAG-017: no key means no knowledge base for this tenant, never
            # another tenant's. Not a failure, so the turn status is untouched.
            detail = "no knowledge base for this tenant"
        elif retrieve:
            bound_s = self._rag.bound_s(_remaining(deadline_at), deadline_s=deadline_s)
            if bound_s * 1000 < RAG_MIN_CALL_MS:
                # Ours, not air-rag-service's: no `downstream` is named.
                self._last_status = TurnStatus.DEGRADED
                self._kb_unavailable = True
                status, detail = StageStatus.DEGRADED, "turn deadline too close to retrieve"
            else:
                try:
                    retrieval = await self._rag.retrieve(
                        masked_text, tenant=principal.tenant, timeout_s=bound_s
                    )
                except RagError as exc:
                    self._last_status = TurnStatus.DEGRADED
                    self._kb_unavailable = True
                    status, detail = StageStatus.DEGRADED, str(exc)
                    downstream = DownstreamService.RAG
                else:
                    self._retrieval_cost_usd = retrieval.usage.est_cost_usd
                    self._sources, dropped = self._select(retrieval.citations)
                    status = StageStatus.OK
                    detail = (
                        f"sources={len(self._sources)} dropped={dropped}"
                        if self._sources
                        else "no relevant sources" + (f" dropped={dropped}" if dropped else "")
                    )
                    _log.info(
                        "retrieval.result",
                        returned=len(retrieval.citations),
                        used=len(self._sources),
                        dropped=dropped,
                        top_score=max((p.score for p in retrieval.citations), default=None),
                    )
        return status, detail, downstream

    async def _turn_inventory(
        self, principal: Principal, *, confirming: bool, deadline_at: float, deadline_s: float
    ) -> Inventory:
        """What this turn may use from air-tools-gateway, fetched at ``PLAN`` so the
        planner chooses from it and the ``route`` event can be checked against it.
        Empty, with no request, when there is nothing it could be used for: no
        gateway, a confirmation turn, a tenant without keys, or a channel whose
        allow-list is empty (``REQ-TGW-004``, ``007``). Never raises (``REQ-TGW-003``)."""
        gateway = self._tools_gateway
        if gateway is None or confirming or not gateway.settings.serves(principal.tenant):
            return Inventory()
        allow = gateway.settings.allow
        if not (allow.customer if principal.channel is Channel.CUSTOMER else allow.business):
            return Inventory()
        bound_s = gateway.bound_s(_remaining(deadline_at), deadline_s=deadline_s)
        if bound_s * 1000 < _LOOKUP_MIN_MS:
            return Inventory(available=False)
        inventory = await gateway.inventory(principal.tenant, timeout_s=bound_s)
        return inventory.offered_to(principal, gateway.settings)

    async def _lookup_part(
        self,
        principal: Principal,
        reads: tuple[Step, ...],
        offered: Inventory,
        *,
        deadline_at: float,
        deadline_s: float,
    ) -> _Part | None:
        """air-tools-gateway's half of ``GATHER`` (spec 0012), or ``None`` when the
        plan has no lookups.

        Every step is checked before anything is sent: its name must be a ``read``
        capability in the turn's inventory, already narrowed to the channel's
        allow-list (``REQ-TGW-004``), and its arguments must validate
        (``REQ-TGW-006``). The subject is the principal's, never an argument
        (``REQ-TGW-008``). The surviving calls run concurrently (``REQ-TGW-010``).
        Each result is screened, cut and later delimited (``REQ-TGW-009``). A step
        that is dropped or fails becomes a notice in the prompt (``REQ-TGW-011``).
        """
        if not reads:
            return None
        gateway = self._tools_gateway
        if gateway is None:
            self._notice(step.capability for step in reads)
            return StageStatus.SKIPPED, "air-tools-gateway is not configured", None
        if not offered.available:
            self._last_status = TurnStatus.DEGRADED
            self._notice(step.capability for step in reads)
            return (
                StageStatus.DEGRADED,
                "air-tools-gateway is unavailable",
                DownstreamService.TOOLS_GATEWAY,
            )

        parts: list[str] = []
        valid: list[tuple[Capability, Step]] = []
        for step in reads:
            capability = offered.get(step.capability)
            if capability is None or capability.kind != "read":
                parts.append(f"{_label(step.capability)} dropped: not offered")
                self._notice([step.capability])
            elif capability.argument_errors(step.arguments):
                parts.append(f"{step.capability} dropped: invalid arguments")
                self._notice([step.capability])
            else:
                valid.append((capability, step))

        outcomes = await asyncio.gather(
            *(
                self._lookup(capability, step, principal, deadline_at, deadline_s)
                for capability, step in valid
            )
        )
        gateway_failed = late = False
        max_chars = gateway.settings.max_result_chars
        for (capability, _step), outcome in zip(valid, outcomes, strict=True):
            name = capability.name
            if outcome is None:
                late = True
                parts.append(f"{name} skipped: turn deadline too close")
                self._notice([name])
            elif isinstance(outcome, str):
                gateway_failed = True
                parts.append(f"{name} {_LOOKUP_FAILURE.get(outcome, 'failed')}")
                self._notice([name])
            else:
                full = orjson.dumps(outcome.data, option=orjson.OPT_SORT_KEYS).decode()
                text = full[:max_chars]
                if screen_input(text).blocked:
                    parts.append(f"{name} dropped: failed the injection screen")
                    self._notice([name])
                    continue
                self._lookups.append((name, text, outcome.truncated or len(full) > max_chars))
                parts.append(f"{name} ok")

        if gateway_failed or late:
            self._last_status = TurnStatus.DEGRADED
            return (
                StageStatus.DEGRADED,
                "; ".join(parts),
                DownstreamService.TOOLS_GATEWAY if gateway_failed else None,
            )
        return StageStatus.OK, "; ".join(parts), None

    async def _proposal_part(
        self,
        principal: Principal,
        write: Step | None,
        offered: Inventory,
        *,
        deadline_at: float,
        deadline_s: float,
    ) -> _Part | None:
        """The planned change, prepared by air-tools-gateway (spec 0012), or ``None``
        when the plan has none. The gateway changes nothing on propose; only its
        answer is kept, server-side (``REQ-TGW-012``, ``013``).

        The step is checked like a lookup, against ``write`` entries of the turn's
        inventory, which already holds none on the business channel
        (``REQ-TGW-026``). A refusal is an answer (``REQ-TGW-028``); any failure is
        ``degraded`` naming the gateway. Either way the turn's answer is fixed text.
        """
        if write is None:
            return None
        self._proposal_answer = _PROPOSE_FAILED
        gateway = self._tools_gateway
        if gateway is None:
            return StageStatus.SKIPPED, "air-tools-gateway is not configured", None
        if not offered.available:
            self._last_status = TurnStatus.DEGRADED
            return (
                StageStatus.DEGRADED,
                "air-tools-gateway is unavailable",
                DownstreamService.TOOLS_GATEWAY,
            )
        capability = offered.get(write.capability)
        if capability is None or capability.kind != "write":
            return StageStatus.OK, f"{_label(write.capability)} dropped: not offered", None
        name = capability.name
        if capability.argument_errors(write.arguments):
            return StageStatus.OK, f"{name} dropped: invalid arguments", None
        bound_s = gateway.bound_s(_remaining(deadline_at), deadline_s=deadline_s)
        if bound_s * 1000 < _LOOKUP_MIN_MS:
            self._last_status = TurnStatus.DEGRADED
            return StageStatus.DEGRADED, f"{name} skipped: turn deadline too close", None
        try:
            proposal = await gateway.propose(
                capability,
                write.arguments,
                tenant=principal.tenant,
                subject=principal.subject,
                timeout_s=bound_s,
            )
        except ToolsGatewayError as exc:
            if exc.reason == "refused" and exc.code in _PROPOSE_REFUSED:
                self._proposal_answer = _PROPOSE_REFUSED[exc.code]
                return StageStatus.OK, f"{name} refused: {exc.code}", None
            self._last_status = TurnStatus.DEGRADED
            return (
                StageStatus.DEGRADED,
                f"{name} {_LOOKUP_FAILURE.get(exc.reason, 'failed')}",
                DownstreamService.TOOLS_GATEWAY,
            )
        self._gateway_proposal = (capability, write, proposal)
        self._proposal_answer = _PROPOSAL_READY.format(summary=_clean(proposal.summary))
        return StageStatus.OK, f"{name} proposed", None

    async def _execution_part(
        self, principal: Principal, pending: PendingProposal | None, *, deadline_at: float
    ) -> _Part | None:
        """Execute a confirmed air-tools-gateway proposal (spec 0012), or ``None`` when
        the turn confirmed nothing, or only the ``/propose`` stand-in.

        One request, never retried (``REQ-TGW-018``), on the execute key with an
        idempotency key derived from the ``prop_`` id (``REQ-TGW-015``, ``016``). The
        answer follows the outcome (``REQ-TGW-017``): a request that went out and
        brought nothing back is *unknown*; one that never went out is *not done*.
        Either is ``degraded``. An execute the gateway answers with ``not_found`` or
        ``proposal_expired`` acted on nothing: *not done*, and ``ok``. ``done``,
        ``pending_approval`` and ``rejected`` are the gateway answering, so ``ok``.
        One ``action.executed`` line is written whatever happened, without arguments
        (``REQ-TGW-021``).
        """
        if pending is None or pending.gateway_proposal_id is None:
            return None
        name = pending.action
        self._execution_answer = _EXECUTE_NOT_DONE
        executor = self._executor
        if executor is None or not executor.settings.serves(principal.tenant):
            self._audit(pending, principal, "not_done")
            return StageStatus.SKIPPED, "air-tools-gateway is not configured", None
        bound_s = executor.bound_s(_remaining(deadline_at))
        if bound_s * 1000 < _LOOKUP_MIN_MS:
            self._last_status = TurnStatus.DEGRADED
            self._audit(pending, principal, "not_done")
            return StageStatus.DEGRADED, f"{name} skipped: turn deadline too close", None
        try:
            outcome = await executor.execute(
                name,
                proposal_id=pending.proposal_id,
                gateway_proposal_id=pending.gateway_proposal_id,
                tenant=principal.tenant,
                subject=pending.subject,
                timeout_s=bound_s,
            )
        except ToolsGatewayError as exc:
            if exc.reason == "refused" and exc.code in EXECUTE_NOT_DONE_CODES:
                # The gateway answering, not failing: nothing was done.
                self._execution_answer = _EXECUTE_NOT_DONE_STALE
                self._audit(pending, principal, "not_done", code=exc.code)
                return StageStatus.OK, f"{name} not done: {exc.code}", None
            self._last_status = TurnStatus.DEGRADED
            sent = exc.reason != "breaker_open"
            self._execution_answer = _EXECUTE_UNKNOWN if sent else _EXECUTE_NOT_DONE
            self._audit(pending, principal, "unknown" if sent else "not_done")
            return (
                StageStatus.DEGRADED,
                f"{name} {_LOOKUP_FAILURE.get(exc.reason, 'failed')}",
                DownstreamService.TOOLS_GATEWAY,
            )
        code: str | None = None
        if outcome.status == "done":
            self._execution_answer = _EXECUTE_DONE
            detail = f"{name} done"
        elif outcome.status == "pending_approval":
            self._execution_answer = _EXECUTE_PENDING
            detail = f"{name} pending_approval"
        else:
            code = outcome.code if outcome.code in _EXECUTE_REJECTED else "other"
            self._execution_answer = _EXECUTE_REJECTED.get(code, _EXECUTE_REJECTED_OTHER)
            detail = f"{name} rejected: {code}"
        self._audit(pending, principal, outcome.status, code=code)
        return StageStatus.OK, detail, None

    def _audit(
        self,
        pending: PendingProposal,
        principal: Principal,
        outcome: str,
        *,
        code: str | None = None,
    ) -> None:
        """``REQ-TGW-021``: one line per execution. This service's ``prop_`` id, not
        the gateway's (``REQ-TGW-013``), and no argument values: they came from a model
        reading customer text."""
        _log.info(
            "action.executed",
            proposal_id=pending.proposal_id,
            capability=pending.action,
            outcome=outcome,
            code=code,
            key_id=principal.key_id,
            tenant=principal.tenant,
        )

    async def _lookup(
        self,
        capability: Capability,
        step: Step,
        principal: Principal,
        deadline_at: float,
        deadline_s: float,
    ) -> ToolResult | str | None:
        """One lookup: its result, the failure's reason word, or ``None`` when the
        turn has no time left for it."""
        assert self._tools_gateway is not None
        bound_s = self._tools_gateway.bound_s(_remaining(deadline_at), deadline_s=deadline_s)
        if bound_s * 1000 < _LOOKUP_MIN_MS:
            return None
        try:
            return await self._tools_gateway.run(
                capability,
                step.arguments,
                tenant=principal.tenant,
                subject=principal.subject,
                timeout_s=bound_s,
            )
        except ToolsGatewayError as exc:
            return exc.reason

    def _notice(self, names: Iterable[str]) -> None:
        for name in names:
            label = _label(name)
            if label not in self._lookup_notices:
                self._lookup_notices.append(label)

    def _select(self, passages: list[Passage]) -> tuple[list[Passage], int]:
        """The passages the prompt may carry, and how many the injection screen
        dropped: highest score first, at least ``min_score``, at most
        ``max_passages``, each snippet cut to ``snippet_chars`` (``REQ-RAG-005``/
        ``006``). A passage that would be blocked as a customer message is an
        injection attempt planted in the knowledge base, so it never reaches the model."""
        assert self._rag is not None
        config = self._rag.settings
        chosen: list[Passage] = []
        dropped = 0
        for passage in sorted(passages, key=lambda p: p.score, reverse=True):
            if passage.score < config.min_score or len(chosen) >= config.max_passages:
                break
            snippet = passage.snippet[: config.snippet_chars]
            if screen_input(snippet).blocked or screen_input(passage.title or "").blocked:
                dropped += 1
                continue
            chosen.append(passage.model_copy(update={"snippet": snippet}))
        return chosen, dropped

    def _lookups_message(self) -> dict[str, str] | None:
        """The system message that carries ``GATHER``'s lookups into the prompt, or
        ``None`` when none were planned (``REQ-TGW-009``/``011``). Each result is its
        own delimited block, so a value inside one cannot pose as another's."""
        if not self._lookups and not self._lookup_notices and not self._change_not_prepared:
            return None
        sections: list[str] = []
        if self._lookups:
            sections.append(f"{_LOOKUPS_HEADER}\n\n{self._lookup_blocks()}")
        if self._lookup_notices:
            sections.append(_LOOKUPS_FAILED.format(names=", ".join(self._lookup_notices)))
        if self._change_not_prepared:
            sections.append(_CHANGE_NOT_PREPARED)
        return {"role": "system", "content": "\n\n".join(sections)}

    def _lookup_blocks(self) -> str:
        """Each lookup result in its own delimited block: what the answer prompt
        carries, and what round 2 of planning is shown (spec 0013 plan §1)."""
        return "\n\n".join(
            delimit(
                f"{name}{' (partial: not every record is shown)' if partial else ''}\n{text}",
                source="tool_result",
            )
            for name, text, partial in self._lookups
        )

    def _sources_message(self) -> dict[str, str] | None:
        """The system message that carries ``GATHER``'s result into the prompt, or
        ``None`` when there is nothing to say (``REQ-RAG-007``/``011``)."""
        if self._kb_unavailable:
            return {"role": "system", "content": _KB_UNAVAILABLE}
        if not self._sources:
            return None
        numbered = "\n\n".join(
            f"[{index}] {safe_title(passage)}\n{passage.snippet}"
            for index, passage in enumerate(self._sources, start=1)
        )
        return {
            "role": "system",
            "content": f"{_SOURCES_HEADER}\n\n{delimit(numbered, source='knowledge_base')}",
        }

    def _citations(self, text: str) -> list[Citation]:
        """One citation per source the final answer cites by a valid ``[n]``, in order
        of first citation (``REQ-RAG-009``). A marker past the last source cites
        nothing."""
        cited: list[Citation] = []
        seen: set[int] = set()
        for match in _CITATION_MARKER.finditer(text):
            index = int(match.group(1))
            if index in seen or not 1 <= index <= len(self._sources):
                continue
            seen.add(index)
            passage = self._sources[index - 1]
            snippet = " ".join(_CONTROL_CHARS.sub(" ", passage.snippet).split())
            cited.append(
                Citation(
                    source_id=passage.source_id[:200],
                    title=safe_title(passage),
                    uri=safe_uri(passage),
                    snippet=snippet[:_CITATION_SNIPPET_CHARS],
                )
            )
        return cited

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

    def _turn_ceiling(self, request: TurnRequest) -> float:
        """The effective per-turn cost ceiling (``REQ-COST-001``): the configured
        one, narrowed (never widened) by the caller's ``options.max_cost_usd``."""
        configured = self._settings.turn.max_cost_usd
        requested = request.options.max_cost_usd
        return configured if requested is None else min(requested, configured)

    def _estimate(self, messages: list[dict[str, str]], max_tokens: int) -> float:
        """An upper bound on what one call could cost (``REQ-COST-004``), priced by
        ``engine/cost.py`` so the planner prices its calls the same way."""
        return estimate_cost(self._settings, messages, max_tokens)

    def _check_ceiling(
        self, request: TurnRequest, call: str, estimate: float, spent: float
    ) -> str | None:
        """``None`` if a call estimated at ``estimate`` fits under the ceiling
        after ``spent``; otherwise the reason, logged."""
        ceiling = self._turn_ceiling(request)
        if spent + estimate <= ceiling:
            return None
        _log.info(
            "turn.cost_ceiling_reached",
            call=call,
            spent_usd=round(spent, 6),
            estimate_usd=round(estimate, 6),
            ceiling_usd=ceiling,
        )
        left = max(ceiling - spent, 0.0)
        return f"cost ceiling: estimated ${estimate:.6f}, ${left:.6f} left"

    def _prose_messages(
        self, principal: Principal, session: Session, masked_text: str
    ) -> tuple[Prompt, list[dict[str, str]]]:
        """The prose synthesis call's messages, for the call and for its estimate.

        The channel alone picks the persona (spec 0006, Article I): nothing in the
        request can name a prompt, and the message itself arrives as delimited data.
        """
        prompt, system = self._prompts.render(prompt_for(principal.channel))
        messages = [{"role": "system", "content": system}]
        # After the persona and before the history (REQ-RAG-007): these sources are for
        # this message only, and are never stored in the session.
        if (sources := self._sources_message()) is not None:
            messages.append(sources)
        if (lookups := self._lookups_message()) is not None:
            messages.append(lookups)
        for turn in session.turns:
            messages.append({"role": turn.role, "content": turn.content})
        # Only the newest turn is demarcated, not replayed history: prior
        # assistant turns are this service's own output, and prior user turns
        # already went through this same boundary once when they were new.
        messages.append(
            {"role": "user", "content": delimit(masked_text, source="customer_message")}
        )
        return prompt, messages

    # ── Pieces ────────────────────────────────────────────────────────────────

    async def _resolve_session(self, session_id: str | None, principal: Principal) -> Session:
        """No id starts a session. An id that does not resolve is a 404, never a
        silent new session: a client that sent one believes it is continuing a
        conversation (``REQ-VAL-001``). Absent, expired and another tenant's are the
        same 404, as on ``GET /v1/sessions/{id}`` (Article IV)."""
        if session_id is None:
            return await self._sessions.create(principal)
        existing = await self._sessions.get(session_id, principal)
        if existing is None:
            raise SessionNotFoundError("No such session.")
        return existing

    async def _resolve_confirmation(
        self, request: TurnRequest, session: Session, principal: Principal
    ) -> PendingProposal | None:
        """The pending proposal this turn confirms, consumed, or ``None``. A proposal
        made by another key, or for another customer, is consumed and confirms
        nothing, exactly as an unknown id does (spec 0012 ``REQ-TGW-019``, spec 0001
        ``REQ-MUT-008``): the answer must not tell a prober the id was real."""
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
        owner = (pending.key_id, pending.subject) == (principal.key_id, principal.subject)
        if not approved or not owner:
            metrics.record_proposal(outcome="rejected")
            return None
        metrics.record_proposal(outcome="confirmed")
        return pending

    async def _propose(self, session: Session, principal: Principal) -> Proposal:
        pending = PendingProposal.create(
            proposal_id=f"{_PROPOSAL_ID_PREFIX}{uuid.uuid4().hex[:12]}",
            action="echo.noop",
            arguments={"note": "a stand-in: nothing executes"},
            risk="low",
            summary=(
                "A stand-in mutation, so the confirm path can be exercised without "
                "changing anything."
            ),
            ttl_seconds=self._settings.session.proposal_ttl_seconds,
            key_id=principal.key_id,
            subject=principal.subject,
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

    async def _propose_gateway(self, session: Session, principal: Principal) -> Proposal:
        """Hold the gateway's proposal on the session and publish this service's own.

        The published :class:`Proposal` carries a ``prop_`` id; the gateway's id is
        kept on the pending proposal only (``REQ-TGW-013``). ``summary`` and ``risk``
        are the gateway's, and the expiry is the earlier of its and this service's
        proposal TTL (``REQ-TGW-014``). The creating key and customer are kept so a
        confirmation can be checked against them at execute (``REQ-TGW-019``).
        """
        assert self._gateway_proposal is not None
        capability, step, offered = self._gateway_proposal
        pending = PendingProposal.create(
            proposal_id=f"{_PROPOSAL_ID_PREFIX}{uuid.uuid4().hex[:12]}",
            action=capability.name,
            arguments=dict(step.arguments),
            risk=offered.risk,
            summary=_clean(offered.summary),
            ttl_seconds=self._settings.session.proposal_ttl_seconds,
            not_after=offered.expires_at,
            gateway_proposal_id=offered.proposal_id,
            key_id=principal.key_id,
            subject=principal.subject,
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

        Confirmation and proposal replies are fixed text, never synthesised: the
        execute outcome or the gateway's summary decides them (spec 0012 plan §1),
        so the deadline and the prompt registry are irrelevant to those branches.

        Raises `ModelGatewayError` on a real synthesis failure, and `TimeoutError`
        when the turn's deadline passes mid-stream — the caller decides the degraded
        fallback; this method's job is only to try.
        """
        if confirmed is not None:
            # The gateway's outcome, chosen in `GATHER`; the stand-in's own text
            # otherwise, since it has nothing to execute.
            outcome.text = self._execution_answer or (
                f"Executed {confirmed.action} (a stand-in: nothing actually changed)."
            )
            return
        if self._proposal_answer is not None:
            outcome.text = self._proposal_answer
            return
        if proposed:
            outcome.text = (
                "That would change something, so here is a proposal rather than an action. "
                "Confirm it by sending the next turn with "
                '`"confirm": {"proposal_id": "…", "approve": true}`.'
            )
            return

        prompt, messages = self._prose_messages(principal, session, masked_text)
        reason = self._check_ceiling(
            request,
            "prose",
            self._estimate(messages, _SYNTHESIS_MAX_TOKENS),
            spent=self._classifier_cost_usd + self._planning_usage.cost_usd,
        )
        if reason is not None:
            raise _CostCeilingError(reason)
        outcome.prompt_version = prompt.ref
        _log.info("turn.synthesised", prompt=prompt.ref, history_turns=len(session.turns))

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
            timeout = _remaining(deadline_at)
            # REQ-MGW-018: given less than half the turn, a timeout is the
            # deadline's. With at least half, the gateway was slow.
            outcome.deadline_bound = (
                timeout is not None and timeout < self._deadline_seconds(request) / 2
            )
            result = await self._gateway.generate(
                messages=messages, max_tokens=_SYNTHESIS_MAX_TOKENS, timeout=timeout
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
        if request.output_schema is None:
            # The business channel's contract is structured output, so it
            # gets a body shaped like one even without a caller-supplied
            # schema — no second model call needed to wrap the same answer.
            outcome.structured = {"answer": text}
        elif self._check_ceiling(
            request,
            "structured",
            self._estimate(_structured_messages(masked_text), _SYNTHESIS_MAX_TOKENS),
            spent=self._classifier_cost_usd
            + self._planning_usage.cost_usd
            + outcome.usage.cost_usd,
        ):
            # REQ-COST-003: the answer was given, so only this field narrows, and
            # the turn's status is unaffected.
            outcome.structured = {"answer": text}
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
                messages=_structured_messages(masked_text),
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

    __slots__ = (
        "deadline_bound",
        "prompt_version",
        "refused",
        "streamed",
        "structured",
        "text",
        "usage",
    )

    def __init__(self) -> None:
        self.text = ""
        self.structured: dict[str, Any] | None = None
        self.usage = Usage(model_calls=0, cost_usd=0.0, cache_hit=False)
        self.prompt_version: str | None = None
        self.refused = False
        self.streamed = False
        #: The prose call was given less than half the turn's deadline, so a
        #: timeout is the deadline's, not the gateway's (REQ-MGW-018).
        self.deadline_bound = False


class _CostCeilingError(Exception):
    """A paid call was not made because its estimate could take the turn past its
    cost ceiling (spec 0008). The message is the stage detail."""


def _structured_messages(masked_text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "Answer as JSON matching the given schema exactly."},
        {"role": "user", "content": delimit(masked_text, source="customer_message")},
    ]


#: One half of ``GATHER``: its status, its detail, and the service to blame.
_Part = tuple[StageStatus, str | None, DownstreamService | None]


def _combine(retrieval: _Part, *others: _Part | None) -> _Part:
    """One ``GATHER`` event from its parts. With nothing else planned, retrieval's
    own; with retrieval not in play, the others' own; else the worst status, every
    detail, and the first service to blame (air-rag first)."""
    planned = [part for part in others if part is not None]
    if not planned:
        return retrieval
    parts = planned if retrieval[0] is StageStatus.SKIPPED else [retrieval, *planned]
    if len(parts) == 1:
        return parts[0]
    statuses = [part[0] for part in parts]
    status = (
        StageStatus.DEGRADED
        if StageStatus.DEGRADED in statuses
        else StageStatus.OK
        if StageStatus.OK in statuses
        else StageStatus.SKIPPED
    )
    return (
        status,
        "; ".join(part[1] for part in parts if part[1]),
        next((part[2] for part in parts if part[2] is not None), None),
    )


def _without_write(plan: Plan) -> Plan:
    """The plan as if it had never planned a change."""
    routes = tuple(route for route in plan.routes if route is not Route.ACTION) or (Route.DIRECT,)
    reason = "direct answer" if routes == (Route.DIRECT,) else plan.reason
    return replace(plan, routes=routes, reason=reason, write=None)


def _clean(text: str) -> str:
    """Downstream text for the customer's eyes: no control characters, one line."""
    return " ".join(_CONTROL_CHARS.sub(" ", text).split())


def _label(name: str) -> str:
    """A planned capability name as it may appear in a prompt or a stage detail: a
    made-up one is model output and is never echoed."""
    return name if is_capability_name(name) else "an unrecognised lookup"


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
