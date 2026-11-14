"""Request and response models for the two conversational routes.

``extra="forbid"`` throughout, matching air-classifier: a typo'd field is a 400,
never a silently ignored option. That matters more here than usual, because most of
these fields *narrow* what a turn may do — an ignored ``max_cost_usd`` would be a
caller believing they had capped their spend when they had not.

Every ceiling in :class:`TurnOptions` is a request to do *less*. The engine clamps
each against the configured maximum, so a caller can lower their own budget and
never raise it.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from air_orchestrator_service.constants import Route, Stage, StageStatus, TurnStatus
from air_orchestrator_service.schemas.common import Usage


def _not_blank(value: str) -> str:
    """Refuse text that is only whitespace (spec 0009, ``REQ-VAL-003``).

    ``min_length=1`` counts spaces, so ``"   "`` was classified, synthesised and
    billed. The text itself is not stripped: what the caller sent is what is masked
    and stored."""
    if not value.strip():
        raise ValueError("must contain more than whitespace")
    return value


#: Turn text: 1 to 32,000 characters, not all of them whitespace.
TurnText = Annotated[str, Field(min_length=1, max_length=32_000), AfterValidator(_not_blank)]

_JSON_SCHEMA_TYPES = frozenset(
    {"string", "number", "integer", "boolean", "object", "array", "null"}
)


def _check_schema(schema: object, path: str = "output_schema") -> None:
    """The structural part of a JSON Schema check (``REQ-VAL-004``): the keywords
    that shape a structured answer, recursively. Not the full metaschema, which
    needs the ``jsonschema`` dependency (``REQ-VAL-007``, spec 0009 Q2)."""
    if isinstance(schema, bool):
        return  # `true`/`false` are valid schemas
    if not isinstance(schema, dict):
        raise ValueError(f"{path} must be a JSON Schema object")
    kind = schema.get("type")
    if kind is not None:
        kinds = kind if isinstance(kind, list) else [kind]
        if not kinds or not all(isinstance(k, str) and k in _JSON_SCHEMA_TYPES for k in kinds):
            raise ValueError(f"{path}.type must be one of {sorted(_JSON_SCHEMA_TYPES)}")
    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise ValueError(f"{path}.properties must be an object")
        for name, sub in properties.items():
            _check_schema(sub, f"{path}.properties.{name}")
    if "items" in schema:
        _check_schema(schema["items"], f"{path}.items")
    required = schema.get("required")
    if required is not None and not (
        isinstance(required, list) and all(isinstance(r, str) for r in required)
    ):
        raise ValueError(f"{path}.required must be a list of strings")


def _valid_schema(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is not None:
        _check_schema(value)
    return value


class TurnOptions(BaseModel):
    """Per-request narrowing. Never widening — see the module docstring."""

    model_config = ConfigDict(extra="forbid")

    deadline_ms: int | None = Field(default=None, ge=100, le=300_000)
    max_cost_usd: float | None = Field(default=None, gt=0)
    allow_routes: list[Route] | None = Field(
        default=None,
        min_length=1,
        description=(
            "Restrict the planner to these routes. Cannot add a route that is not live. "
            "Omit it for the default; an empty list is refused."
        ),
    )
    use_cache: bool | None = None
    redact_pii: bool | None = Field(
        default=None,
        description=(
            "Business channel only. A customer-channel request setting this is refused "
            "rather than ignored, so a caller cannot believe they turned it off."
        ),
    )
    include_trace: bool = True


class Confirmation(BaseModel):
    """The only way to execute a proposed mutation — docs/02-lld.md §8.

    A structured field rather than free text on purpose. Prose that reads as
    agreement, in any language or phrasing, cannot produce this object, which is what
    makes a successful prompt injection in the proposing turn a dead end rather than
    a write.
    """

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=128)
    approve: bool


class ChatRequest(BaseModel):
    """A conversational turn on the customer channel."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(
        default=None,
        max_length=128,
        description="Absent starts a new session, whose id is returned on `turn.start`.",
    )
    message: TurnText
    confirm: Confirmation | None = None
    options: TurnOptions | None = None


class QueryRequest(BaseModel):
    """A business-channel query. Answers as validated structured output."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, max_length=128)
    query: TurnText
    output_schema: Annotated[dict[str, Any] | None, AfterValidator(_valid_schema)] = Field(
        default=None,
        description="JSON Schema the answer must satisfy; enforced by the output guardrail.",
    )
    options: TurnOptions | None = None


# ── Response parts ────────────────────────────────────────────────────────────


class Citation(BaseModel):
    """One retrieved source the answer actually used.

    Only sources that contributed are cited. Listing everything retrieved would make
    the citation list a measure of recall rather than of grounding.
    """

    source_id: str
    title: str
    uri: str | None = None
    snippet: str | None = None


class Proposal(BaseModel):
    """A mutation air-action has validated and priced but **not** performed."""

    proposal_id: str
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk: str = Field(description="air-action's own assessment: low | medium | high.")
    summary: str = Field(description="What executing this would do, in the user's terms.")
    expires_at: str = Field(description="ISO-8601. After this the proposal cannot be confirmed.")


class StageRecord(BaseModel):
    """One pipeline step's outcome, for the optional trace."""

    stage: Stage
    status: StageStatus
    latency_ms: float
    detail: str | None = None


class TurnResult(BaseModel):
    """The non-streaming envelope.

    Collected from the same event sequence a streaming caller receives, so the two
    surfaces cannot drift: there is one engine, and this is what you get if you
    decline to watch it work.
    """

    turn_id: str
    session_id: str
    answer: str
    structured: dict[str, Any] | None = None
    citations: list[Citation] = Field(default_factory=list)
    proposal: Proposal | None = None
    routes: list[Route] = Field(default_factory=list)
    grounded: bool = False
    refusal: bool = False
    escalated: bool = False
    escalation_ref: str | None = None
    degraded: list[str] = Field(
        default_factory=list,
        description="Downstreams that were unavailable, so a caller can see what was missing.",
    )
    status: TurnStatus = TurnStatus.OK
    usage: Usage = Field(default_factory=Usage)
    trace: list[StageRecord] | None = None
