"""What a turn will do, decided once, at ``PLAN`` — spec 0012 §3, plan §1.

A :class:`Plan` is the routes, the reason given for them, whether to retrieve from
air-rag-service, the air-tools-gateway ``read`` steps to run in ``GATHER``, and at
most one ``write`` step to propose. The engine asks a :class:`Planner` for it and
then carries it out; it does not decide anything about intent itself.

**Why a seam, before there is a second planner.** Specs 0012 and 0013 are built
against a plan, not against a model. With the decision behind a protocol, spec
0012's lookups and proposals are driven in tests by a fixed plan (the "injected
plan" spec 0012 §3 promises), and spec 0013's ``ModelPlanner`` slots in behind the
same call. Without it, both would have to grow inside ``TurnEngine._pipeline``.

**Why :class:`RulePlanner` emits no gateway step.** It is today's rule, moved here
unchanged: a customer turn retrieves when air-rag-service serves the tenant, and
the literal ``/propose`` exercises the proposal path for a key that may propose.
A rule cannot tell "where is my order" from "tell me a joke", so it never claims to
(Article V): with no model planner, nothing in spec 0012 is reachable from a real
turn, and that is the honest state of this build.

**Why a confirmation turn is not planned.** Whether a turn executes is decided by
the structured ``confirm`` field alone (Article II). The planner is told the turn
is a confirmation and plans nothing, so no reading of its text can add a step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from air_orchestrator_service.clients.tools_gateway import Inventory
from air_orchestrator_service.constants import Channel, DownstreamService, Route
from air_orchestrator_service.schemas.common import Principal

__all__ = [
    "PROPOSE_TRIGGER",
    "Plan",
    "PlanContext",
    "Planner",
    "RulePlanner",
    "Step",
]

#: Saying this in a message asks the engine to propose a mutation, so the
#: propose -> confirm -> execute path can be exercised end to end before
#: air-tools-gateway's proposals are wired (spec 0012 T8). A stand-in, and
#: :class:`RulePlanner` is the only thing that reads it.
PROPOSE_TRIGGER: Final[str] = "/propose"


@dataclass(frozen=True, slots=True)
class Step:
    """One air-tools-gateway call: a capability name and its arguments. The engine
    checks both against the turn's inventory before anything is sent
    (``REQ-TGW-006``)."""

    capability: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlanContext:
    """What a planner may look at. ``text`` is already input-masked."""

    text: str
    principal: Principal
    #: The turn carries a resolved ``confirm``: plan nothing.
    confirming: bool
    #: air-rag-service is configured and holds a key for this tenant.
    rag_serves: bool
    #: What this turn may use from air-tools-gateway: the tenant's inventory, narrowed
    #: to the channel's allow-list and the principal (spec 0012, ``REQ-TGW-004``).
    offered: Inventory = field(default_factory=Inventory)


@dataclass(frozen=True, slots=True)
class Plan:
    routes: tuple[Route, ...]
    reason: str
    retrieve: bool = False
    reads: tuple[Step, ...] = ()
    write: Step | None = None
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


class Planner(Protocol):
    async def plan(self, ctx: PlanContext) -> Plan: ...


class RulePlanner:
    """Today's routing rule. See the module docstring."""

    async def plan(self, ctx: PlanContext) -> Plan:
        if ctx.confirming:
            return Plan(routes=(Route.DIRECT,), reason="direct answer")
        # A key without ``allow_actions`` is answered as a direct turn, not refused
        # (spec 0001, ``REQ-MUT-012``): a 403 would tell a prober which keys can
        # write, and the same silence is what Article II asks of a stale confirm.
        if ctx.principal.allow_actions and PROPOSE_TRIGGER in ctx.text.lower():
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
