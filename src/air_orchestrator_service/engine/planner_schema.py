"""The planner's output, as a schema built per turn and a model that checks it — spec 0013.

The planning call is asked for JSON with ``json_schema`` (air-model-gateway has no
``tools`` field), and the answer is parsed here. Both rounds have their own pair.

**Why the schema is built per turn.** A static schema would let the model name any
capability and lose the turn's lookups to a typo. Here ``capability`` is an ``enum`` of
the names this turn was offered: read names in ``reads``, write names in ``propose``.
With no write on offer (the business channel, a key without ``allow_actions``),
``propose`` can only be ``null``. The model's choices are constrained before
validation, not only after (plan §5).

**Why the output is still checked afterwards.** air-model-gateway forwards a schema
and does not enforce it, so a provider that ignores it can return anything.
:func:`parse_plan` and :func:`parse_follow_up` check the shape (``REQ-PLN-003``): an
answer that fails is no plan at all, and the turn falls back to the rule plan. They do
*not* check names against the menu or arguments against a capability's schema. Spec
0012's checks in ``GATHER`` do that for every step, a model's or a fixed plan's alike
(``REQ-PLN-006``), so a model plan gets no path around them.

**Why round 2 has no ``reads``.** The follow-up may only propose, or not. Leaving the
field out of its schema puts "no further reads" in the grammar itself
(``REQ-PLN-007``), and its model refuses an answer that adds one anyway.
"""

from __future__ import annotations

import json
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError

from air_orchestrator_service.clients.tools_gateway import Inventory

__all__ = [
    "REASON_MAX_CHARS",
    "FollowUpOutput",
    "PlanOutput",
    "StepOutput",
    "follow_up_schema",
    "parse_follow_up",
    "parse_plan",
    "plan_schema",
]

REASON_MAX_CHARS: Final[int] = 200
_NAME_MAX_CHARS: Final[int] = 128


class StepOutput(BaseModel):
    """One step as the model wrote it. ``arguments`` is checked later, against the
    named capability's own ``input_schema``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: StrictStr = Field(min_length=1, max_length=_NAME_MAX_CHARS)
    arguments: dict[str, Any]


class PlanOutput(BaseModel):
    """Round 1 (``REQ-PLN-003``). Every field is required, as in the schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reads: list[StepOutput]
    propose: StepOutput | None
    needs_results: StrictBool
    reason: StrictStr = Field(max_length=REASON_MAX_CHARS)


class FollowUpOutput(BaseModel):
    """Round 2 (``REQ-PLN-007``): a proposal or none, and nothing else."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    propose: StepOutput | None
    reason: StrictStr = Field(max_length=REASON_MAX_CHARS)


def _names(offered: Inventory, kind: str) -> list[str]:
    return sorted(c.name for c in offered.capabilities if c.kind == kind)


def _step_schema(names: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["capability", "arguments"],
        "properties": {
            "capability": {"type": "string", "enum": names},
            "arguments": {"type": "object"},
        },
    }


def _propose_schema(offered: Inventory) -> dict[str, Any]:
    writes = _names(offered, "write")
    if not writes:
        return {"type": "null"}
    return {"anyOf": [_step_schema(writes), {"type": "null"}]}


_REASON: Final[dict[str, Any]] = {"type": "string", "maxLength": REASON_MAX_CHARS}


def plan_schema(offered: Inventory, *, max_reads: int) -> dict[str, Any]:
    """Round 1's schema: ``reads`` from the offered read names, at most ``max_reads``;
    ``propose`` from the offered write names, or ``null`` only."""
    reads = _names(offered, "read")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reads", "propose", "needs_results", "reason"],
        "properties": {
            "reads": {
                "type": "array",
                "maxItems": max_reads if reads else 0,
                "items": _step_schema(reads),
            },
            "propose": _propose_schema(offered),
            "needs_results": {"type": "boolean"},
            "reason": _REASON,
        },
    }


def follow_up_schema(offered: Inventory) -> dict[str, Any]:
    """Round 2's schema: no ``reads`` field at all."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["propose", "reason"],
        "properties": {"propose": _propose_schema(offered), "reason": _REASON},
    }


def _load(content: str) -> object:
    try:
        return json.loads(content)
    except (json.JSONDecodeError, RecursionError):
        return None


def parse_plan(content: str, *, max_reads: int) -> PlanOutput | None:
    """Round 1's answer, or ``None`` when it is not a valid plan."""
    try:
        plan = PlanOutput.model_validate(_load(content))
    except ValidationError:
        return None
    return plan if len(plan.reads) <= max_reads else None


def parse_follow_up(content: str) -> FollowUpOutput | None:
    """Round 2's answer, or ``None`` when it is not valid."""
    try:
        return FollowUpOutput.model_validate(_load(content))
    except ValidationError:
        return None
