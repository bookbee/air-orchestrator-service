"""What a turn will do, decided once, at ``PLAN`` — specs 0012 §3 and 0013.

A :class:`Plan` is the routes, the reason given for them, whether to retrieve from
air-rag-service, the air-tools-gateway ``read`` steps to run in ``GATHER``, and at
most one ``write`` step to propose. The engine asks a :class:`Planner` for it and
then carries it out; it does not decide anything about intent itself. The planner
answers with a :class:`PlanOutcome`: the plan, plus what ``PLAN``'s stage event says
about how it was made.

**Why a seam.** Specs 0012 and 0013 are built against a plan, not against a model.
Spec 0012's lookups and proposals are driven in tests by a fixed plan, and
:class:`ModelPlanner` slots in behind the same call. Every step either one produces
goes through the same checks in ``GATHER`` (``REQ-PLN-006``): the planner never calls
air-tools-gateway, and a model's plan gets no exemption a fixed plan does not.

**Why :class:`RulePlanner` emits no gateway step.** It is the routing rule from before
there was a planner: a customer turn retrieves when air-rag-service serves the tenant,
and the literal ``/propose`` exercises the proposal path for a key that may propose. A
rule cannot tell "where is my order" from "tell me a joke", so it never claims to
(Article V). With the model planner off, nothing in spec 0012 is reachable from a real
turn, and ``PLAN`` says so: ``skipped``, ``planner disabled`` (``REQ-PLN-011``).

**Why the model planner falls back to the rule without the stand-in.** A planning call
that fails, times out, is invalid or does not fit the budget falls back to the rule
plan, and the turn still answers (``REQ-PLN-004``). That fallback is built with
``stand_in=False``: a deployment that turned the planner on has retired ``/propose``,
and a planning failure must not turn it back on (``REQ-PLN-010``).

**Why the prompt has no identity in it.** :func:`planning_messages` takes the masked
text, the history, the classifier's labels and the menu, and nothing else. The
principal never reaches it, so the subject, tenant, keys and token cannot be rendered
into a prompt by a later edit to it (``REQ-PLN-002``). A capability that needs the
customer gets them from the envelope (``REQ-TGW-008``).

**Why two rounds and never three.** "Cancel my last order" needs the order number from
a lookup before the change can be proposed, so a round-1 plan may set
``needs_results``. The engine then asks :meth:`Planner.follow_up` once, after the reads
return, and round 2's schema has no ``reads``: it can only propose or not
(``REQ-PLN-007``). Unbounded loops are how agents run up cost; "compare, then cancel"
is the deepest real case and fits.

**Why a confirmation turn is not planned.** Whether a turn executes is decided by the
structured ``confirm`` field alone (Article II). The planner is told the turn is a
confirmation and plans nothing, so no reading of its text can add a step.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field, replace
from typing import Any, Final, Protocol

import orjson

from air_orchestrator_service.clients.model_gateway import (
    GenerateResult,
    ModelGatewayClient,
    ModelGatewayError,
)
from air_orchestrator_service.clients.tools_gateway import Inventory, is_capability_name
from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import Channel, DownstreamService, Route, StageStatus
from air_orchestrator_service.engine.cost import estimate_cost
from air_orchestrator_service.engine.planner_schema import (
    StepOutput,
    follow_up_schema,
    parse_follow_up,
    parse_plan,
    plan_schema,
)
from air_orchestrator_service.guardrails.boundary import delimit
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.observability.logging import get_logger, safe_text_fields
from air_orchestrator_service.prompts.registry import PLANNER, PromptRegistry
from air_orchestrator_service.schemas.common import Principal, Usage

__all__ = [
    "PROPOSE_TRIGGER",
    "FollowUp",
    "ModelPlanner",
    "Plan",
    "PlanContext",
    "PlanOutcome",
    "Planner",
    "RulePlanner",
    "Step",
    "planning_messages",
]

_log = get_logger(__name__)

#: Saying this in a message asks the engine to propose the no-op stand-in mutation,
#: so the propose -> confirm -> execute path can be exercised without a model. Only
#: :class:`RulePlanner` built with ``stand_in=True`` reads it.
PROPOSE_TRIGGER: Final[str] = "/propose"

#: The shortest planning call worth making; below it ``PLAN`` falls back.
_PLAN_MIN_MS: Final[int] = 100

_DISABLED: Final[str] = "planner disabled"
_NOTHING_OFFERED: Final[str] = "no capabilities offered"
_CONFIRMING: Final[str] = "nothing to plan: a confirmation turn"


@dataclass(frozen=True, slots=True)
class Step:
    """One air-tools-gateway call: a capability name and its arguments. The engine
    checks both against the turn's inventory before anything is sent
    (``REQ-TGW-006``)."""

    capability: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlanContext:
    """What a planner may look at. ``text`` and ``history`` are already input-masked.

    ``principal`` is here for the rule, which routes on channel and
    ``allow_actions``. :func:`planning_messages` never receives it (``REQ-PLN-002``).
    """

    text: str
    principal: Principal
    #: The turn carries a resolved ``confirm``: plan nothing.
    confirming: bool
    #: air-rag-service is configured and holds a key for this tenant.
    rag_serves: bool
    #: What this turn may use from air-tools-gateway: the tenant's inventory, narrowed
    #: to the channel's allow-list and the principal (spec 0012, ``REQ-TGW-004``).
    offered: Inventory = field(default_factory=Inventory)
    #: The session's stored messages, ``(role, content)``, oldest first, masked as stored.
    history: tuple[tuple[str, str], ...] = ()
    #: air-classifier's labels for this message, already slug-checked, when it ran.
    intent: str | None = None
    sentiment: str | None = None
    #: The turn's whole deadline, and the ``perf_counter`` instant it runs out.
    deadline_s: float = 0.0
    deadline_at: float | None = None
    #: What the turn's cost ceiling leaves for planning, after the answer is reserved.
    max_cost_usd: float = math.inf


@dataclass(frozen=True, slots=True)
class Plan:
    routes: tuple[Route, ...]
    reason: str
    retrieve: bool = False
    reads: tuple[Step, ...] = ()
    write: Step | None = None
    #: Round 1 asked to be shown its reads' results before proposing (spec 0013).
    needs_results: bool = False
    #: The ``/propose`` stand-in: propose the no-op echo action. Never set
    #: together with ``write``.
    stand_in_proposal: bool = False

    @property
    def capabilities(self) -> list[str]:
        """What the ``route`` event names: only what this plan uses
        (``REQ-TGW-025``), never the inventory it chose from."""
        used = [DownstreamService.RAG.value] if self.retrieve else []
        used.extend(step.capability for step in self.reads)
        if self.write is not None:
            used.append(self.write.capability)
        return used


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    """A plan, and what ``PLAN``'s stage event reports about how it was made
    (``REQ-PLN-011``). ``usage`` is what planning spent, ``None`` when no call was made."""

    plan: Plan
    status: StageStatus = StageStatus.OK
    detail: str | None = None
    downstream: DownstreamService | None = None
    usage: Usage | None = None
    #: The planner prompt's ``name:version`` when a planning call was made.
    prompt_ref: str | None = None


@dataclass(frozen=True, slots=True)
class FollowUp:
    """Round 2's answer: the step to propose, or none, and ``GATHER``'s detail for it.
    ``failed`` means the turn could not get an answer, so it is degraded and the reply
    says the change could not be prepared."""

    write: Step | None
    detail: str
    failed: bool = False
    downstream: DownstreamService | None = None
    usage: Usage | None = None


class Planner(Protocol):
    async def plan(self, ctx: PlanContext) -> PlanOutcome: ...

    async def follow_up(self, ctx: PlanContext, first: Plan, results: str) -> FollowUp: ...


class RulePlanner:
    """The routing rule. See the module docstring. ``stand_in`` keeps the ``/propose``
    trigger; :class:`ModelPlanner` builds its fallback without it."""

    def __init__(self, *, stand_in: bool = True) -> None:
        self._stand_in = stand_in

    async def plan(self, ctx: PlanContext) -> PlanOutcome:
        return PlanOutcome(self.rule(ctx), StageStatus.SKIPPED, _DISABLED)

    async def follow_up(self, ctx: PlanContext, first: Plan, results: str) -> FollowUp:
        return FollowUp(None, "round 2: no change")

    def rule(self, ctx: PlanContext) -> Plan:
        if ctx.confirming:
            return Plan(routes=(Route.DIRECT,), reason="direct answer")
        # A key without ``allow_actions`` is answered as a direct turn, not refused
        # (spec 0001, ``REQ-MUT-012``): a 403 would tell a prober which keys can
        # write, and the same silence is what Article II asks of a stale confirm.
        if self._stand_in and ctx.principal.allow_actions and PROPOSE_TRIGGER in ctx.text.lower():
            return Plan(
                routes=(Route.ACTION,),
                reason=f"'{PROPOSE_TRIGGER}' present, exercising the proposal path",
                stand_in_proposal=True,
            )
        if ctx.rag_serves and ctx.principal.channel is Channel.CUSTOMER:
            return Plan(
                routes=(Route.RAG,),
                reason="customer message: answer from the knowledge base",
                retrieve=True,
            )
        return Plan(routes=(Route.DIRECT,), reason="direct answer")


def _menu(ctx: PlanContext) -> Inventory:
    """What the planner may choose from: the turn's offer, less writes the principal
    could never propose (the business channel, a key without ``allow_actions``), so
    its schema's ``propose`` is ``null``-only for them."""
    can_write = ctx.principal.allow_actions and ctx.principal.channel is Channel.CUSTOMER
    return Inventory(
        available=ctx.offered.available,
        capabilities=tuple(c for c in ctx.offered.capabilities if can_write or c.kind != "write"),
    )


def _catalogue(menu: Inventory) -> str:
    entries = [
        {
            "name": c.name,
            "kind": c.kind,
            "description": c.description,
            "input_schema": dict(c.input_schema),
        }
        for c in sorted(menu.capabilities, key=lambda c: c.name)
    ]
    return orjson.dumps(entries, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2).decode()


def planning_messages(
    system: str,
    *,
    text: str,
    menu: Inventory,
    history: tuple[tuple[str, str], ...],
    intent: str | None,
    sentiment: str | None,
) -> list[dict[str, str]]:
    """Round 1's messages (plan §1 step 2). Takes no principal: see the module
    docstring. Everything not written by this service is delimited."""
    messages = [
        {"role": "system", "content": system},
        {
            "role": "system",
            "content": "Capabilities on offer for this turn:\n\n"
            + delimit(_catalogue(menu), source="capabilities"),
        },
    ]
    context: list[str] = []
    if history:
        lines = "\n".join(
            f"{'customer' if role == 'user' else 'assistant'}: {content}"
            for role, content in history
        )
        context.append("Recent messages, oldest first:\n\n" + delimit(lines, source="conversation"))
    if intent or sentiment:
        context.append(
            f"A classifier read this message as: intent={intent or 'unknown'}, "
            f"sentiment={sentiment or 'unknown'}."
        )
    if context:
        messages.append({"role": "system", "content": "\n\n".join(context)})
    messages.append({"role": "user", "content": delimit(text, source="customer_message")})
    return messages


def _step(raw: StepOutput) -> Step:
    return Step(raw.capability, dict(raw.arguments))


def _label(name: str) -> str:
    return name if is_capability_name(name) else "an unrecognised capability"


def _usage(result: GenerateResult) -> Usage:
    return Usage(
        model_calls=1,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        total_tokens=result.usage.prompt_tokens + result.usage.completion_tokens,
        cost_usd=result.cost_usd,
        planning_cost_usd=result.cost_usd,
    )


@dataclass(frozen=True, slots=True)
class _Call:
    """One planning call's result: the content, or why there is none."""

    content: str | None
    outcome: str
    detail: str
    downstream: DownstreamService | None = None
    usage: Usage | None = None


class ModelPlanner:
    """Plans a turn with one schema-constrained call through air-model-gateway, and a
    second when round 1 asks for its reads' results (spec 0013). Built once in
    ``create_app`` when ``planner.enabled``."""

    def __init__(
        self, settings: Settings, gateway: ModelGatewayClient, prompts: PromptRegistry
    ) -> None:
        self._settings = settings
        self._config = settings.planner
        self._gateway = gateway
        self._prompts = prompts
        self._fallback = RulePlanner(stand_in=False)

    async def plan(self, ctx: PlanContext) -> PlanOutcome:
        fallback = self._fallback.rule(ctx)
        if ctx.confirming:
            return PlanOutcome(fallback, StageStatus.SKIPPED, _CONFIRMING)
        menu = _menu(ctx)
        if not menu.capabilities:
            return PlanOutcome(fallback, StageStatus.SKIPPED, _NOTHING_OFFERED)

        prompt, system = self._prompts.render(PLANNER)
        messages = planning_messages(
            system,
            text=ctx.text,
            menu=menu,
            history=ctx.history[-self._config.history_turns :]
            if self._config.history_turns
            else (),
            intent=ctx.intent,
            sentiment=ctx.sentiment,
        )
        call = await self._call(messages, plan_schema(menu, max_reads=self._config.max_reads), ctx)
        parsed = (
            parse_plan(call.content, max_reads=self._config.max_reads)
            if call.content is not None
            else None
        )
        if call.content is not None and parsed is None:
            call = replace(call, outcome="invalid", detail="planner returned an invalid plan")
        metrics.record_planner_call(round=1, outcome=call.outcome if parsed is None else "ok")
        if parsed is None:
            return PlanOutcome(
                fallback,
                StageStatus.DEGRADED,
                call.detail,
                call.downstream,
                call.usage,
                prompt.ref,
            )

        reads = tuple(_step(read) for read in parsed.reads)
        write = _step(parsed.propose) if parsed.propose is not None else None
        routes = [Route.RAG] if fallback.retrieve else []
        routes += [Route.TOOLS] if reads else []
        routes += [Route.ACTION] if write is not None else []
        plan = Plan(
            routes=tuple(routes) or (Route.DIRECT,),
            # Fixed text: the model's `reason` never reaches the client (REQ-PLN-013).
            reason="planned: "
            + (", ".join(_describe(reads, write, fallback.retrieve)) or "a direct answer"),
            retrieve=fallback.retrieve,
            reads=reads,
            write=write,
            needs_results=parsed.needs_results,
        )
        _log.info(
            "planner.plan",
            round=1,
            prompt=prompt.ref,
            reads=[_label(step.capability) for step in reads],
            propose=_label(write.capability) if write is not None else None,
            needs_results=parsed.needs_results,
            **_reason_fields(parsed.reason, self._settings),
        )
        detail = (
            f"reads={len(reads)} propose={'yes' if write else 'no'} "
            f"needs_results={'yes' if parsed.needs_results else 'no'}"
        )
        return PlanOutcome(plan, StageStatus.OK, detail, None, call.usage, prompt.ref)

    async def follow_up(self, ctx: PlanContext, first: Plan, results: str) -> FollowUp:
        menu = _menu(ctx)
        prompt, system = self._prompts.render(PLANNER)
        messages = planning_messages(
            system,
            text=ctx.text,
            menu=menu,
            history=ctx.history[-self._config.history_turns :]
            if self._config.history_turns
            else (),
            intent=ctx.intent,
            sentiment=ctx.sentiment,
        )
        round_one = orjson.dumps(
            {
                "reads": [
                    {"capability": s.capability, "arguments": s.arguments} for s in first.reads
                ],
                "needs_results": first.needs_results,
            },
            option=orjson.OPT_SORT_KEYS,
        ).decode()
        messages.insert(
            -1,
            {
                "role": "system",
                "content": "This is the second round. Your first plan:\n\n"
                + delimit(round_one, source="first_plan")
                + "\n\nThe results of its reads:\n\n"
                + results,
            },
        )
        call = await self._call(messages, follow_up_schema(menu), ctx)
        parsed = parse_follow_up(call.content) if call.content is not None else None
        if call.content is not None and parsed is None:
            call = replace(call, outcome="invalid", detail="planner returned an invalid plan")
        metrics.record_planner_call(round=2, outcome=call.outcome if parsed is None else "ok")
        if parsed is None:
            return FollowUp(None, f"round 2: {call.detail}", True, call.downstream, call.usage)
        write = _step(parsed.propose) if parsed.propose is not None else None
        _log.info(
            "planner.plan",
            round=2,
            prompt=prompt.ref,
            propose=_label(write.capability) if write is not None else None,
            **_reason_fields(parsed.reason, self._settings),
        )
        detail = (
            f"round 2: propose {_label(write.capability)}"
            if write is not None
            else "round 2: no change"
        )
        return FollowUp(write, detail, False, None, call.usage)

    async def _call(
        self, messages: list[dict[str, str]], schema: dict[str, Any], ctx: PlanContext
    ) -> _Call:
        """One bounded, budgeted planning call (``REQ-PLN-005``). Every way it can fail
        becomes a :class:`_Call` with no content; it never raises. The caller counts
        it, once it knows whether the content parsed (``REQ-PLN-012``)."""
        estimate = estimate_cost(self._settings, messages, self._config.max_tokens)
        if estimate > ctx.max_cost_usd:
            return _Call(None, "skipped_budget", "cost ceiling: nothing left to plan")
        bound_s = self._config.deadline_share * ctx.deadline_s
        if ctx.deadline_at is not None:
            bound_s = min(bound_s, ctx.deadline_at - time.perf_counter())
        if bound_s * 1000 < _PLAN_MIN_MS:
            return _Call(None, "skipped_budget", "turn deadline too close to plan")
        try:
            async with asyncio.timeout(bound_s):
                result = await self._gateway.generate(
                    messages=messages,
                    max_tokens=self._config.max_tokens,
                    json_schema=schema,
                    schema_name="plan",
                    model_class=self._config.model_class,
                    timeout=bound_s,
                )
        except TimeoutError:
            return _Call(
                None,
                "timeout",
                f"planner call timed out after {bound_s:.2f}s",
                DownstreamService.MODEL_GATEWAY,
            )
        except ModelGatewayError as exc:
            # `str(exc)` is built from a status and a slug-checked code, never a body.
            return _Call(
                None,
                "timeout" if exc.timed_out else "error",
                f"planner call failed: {exc}",
                DownstreamService.MODEL_GATEWAY,
            )
        usage = _usage(result)
        if result.refusal:
            return _Call(
                None, "invalid", "planner call refused", DownstreamService.MODEL_GATEWAY, usage
            )
        if result.cut_off:
            return _Call(
                None, "invalid", "planner answer cut off", DownstreamService.MODEL_GATEWAY, usage
            )
        return _Call(result.content, "ok", "", DownstreamService.MODEL_GATEWAY, usage)


def _describe(reads: tuple[Step, ...], write: Step | None, retrieve: bool) -> list[str]:
    parts = []
    if retrieve:
        parts.append("the knowledge base")
    if reads:
        parts.append(f"{len(reads)} lookup{'s' if len(reads) != 1 else ''}")
    if write is not None:
        parts.append("a proposed change")
    return parts


def _reason_fields(reason: str, settings: Settings) -> dict[str, object]:
    """The model's ``reason`` as a log field: it was written from customer text, so it
    is logged like customer text (``safe_text_fields``), and only ever logged
    (``REQ-PLN-013``)."""
    return {f"reason_{key}": value for key, value in safe_text_fields(reason, settings).items()}
