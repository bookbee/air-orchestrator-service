"""air-tools-gateway clients: inventory, lookups, proposals and execution, spec 0012.

The contract is ``docs/specs/0012-tools-gateway/contract.openapi.yaml``. The gateway
is a plain REST service in front of the business systems. It decides nothing about
language: it lists what it can do (``GET /v1/capabilities``), runs a lookup
(``POST /v1/tools/{name}``) and prepares a change without making it
(``POST /v1/actions/{name}/propose``). Executing one is :class:`ActionExecutor`'s
job, on a different key, and :class:`ToolsGatewayClient` has no method that could do
it (plan §2).

**Why two clients for one service.** The execute key must be out of reach of the code
a planner drives, so "the read path cannot execute" is a property of the module
surface rather than a convention (``REQ-TGW-016``). The two also have separate
breakers: five failed lookups must not refuse a customer's confirmed change, and a
failing execute must not switch lookups off. Both carry the ``air-tools-gateway``
label. :class:`ActionExecutor`'s HTTP client is built with no default key, so every
request it sends names the execute key explicitly, and no request it sends can fall
back to the read key.

**Why execute is never retried, and its key is derived.** The client base makes one
attempt (spec 0010), and the engine has consumed the pending proposal before calling
(``REQ-TGW-018``). The ``Idempotency-Key`` is ``airo-<proposal id>``, so if the same
proposal ever did reach the gateway twice it would answer the first response again
rather than act twice (``REQ-TGW-015``).

**Why the inventory is held per tenant.** The gateway answers ``/v1/capabilities``
per key, and each key belongs to one tenant (``REQ-TGW-007``). Two tenants can see
different capabilities, so one cache entry serving both would let one tenant's
turn plan against another's inventory (``REQ-TGW-001``).

**Why a refresh is attempted at most once per TTL, success or not.** A gateway
that is down would otherwise be asked for its inventory on every turn of every
tenant. After a failed refresh, the last good inventory keeps serving until
``inventory_grace_s`` from *its* fetch, then nothing does (``REQ-TGW-003``). One
lock per tenant, so a cold cache under concurrent turns is fetched once.

**Why each entry is checked, and a bad one dropped alone.** An inventory entry
becomes a choice offered to a model and a JSON Schema the engine trusts to keep
arguments in shape. A malformed one is dropped and logged once, by name, and the
rest kept (``REQ-TGW-002``): one bad capability must not take the others down,
and must not log on every refresh either. A schema must say
``additionalProperties: false`` and declare neither ``subject`` nor ``tenant``,
because identity travels in the envelope, never in arguments (``REQ-TGW-008``).
A top-level ``patternProperties`` is refused for the same reason: it could admit
``subject`` through a pattern, and spec 0012 AC-2 relies on no accepted schema
admitting it at all.

**Why ``run`` and ``propose`` take a :class:`Capability`, not a name.** The metric
labels a call by capability name (``REQ-TGW-022``). Only names that passed the
checks above, at most 64 per tenant, can reach a label, so its cardinality is
bounded by the inventory, not by whatever a planner emits.

**Why the subject is sent only when required.** A lookup that does not concern one
customer has no business receiving one; ``subject`` is ``null`` for it, as the
contract says.

Everything else comes from ``clients/base.py`` (spec 0010): headers, one attempt,
the four failure reasons, the downstream metric and the breaker.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field

from air_orchestrator_service.clients.base import DownstreamClient, DownstreamError
from air_orchestrator_service.config import ToolsGatewaySettings
from air_orchestrator_service.constants import Channel, DownstreamService
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.observability.logging import get_logger
from air_orchestrator_service.schemas.common import Principal

__all__ = [
    "EXECUTE_NOT_DONE_CODES",
    "MAX_CAPABILITIES",
    "READ_REFUSAL_CODES",
    "REFUSAL_CODES",
    "ActionExecutor",
    "Capability",
    "ExecuteOutcome",
    "GatewayProposal",
    "Inventory",
    "ToolResult",
    "ToolsGatewayClient",
    "ToolsGatewayError",
    "idempotency_key",
    "is_capability_name",
]

logger = get_logger(__name__)

_INVENTORY_PATH: Final[str] = "/v1/capabilities"
#: At most this many entries are kept per tenant (``REQ-TGW-002``).
MAX_CAPABILITIES: Final[int] = 64
_NAME: Final = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_NAME_CHARS: Final[int] = 64
_DESCRIPTION_CHARS: Final[int] = 1000
_IDENTITY_FIELDS: Final[frozenset[str]] = frozenset({"subject", "tenant"})
_RISKS: Final[frozenset[str]] = frozenset({"low", "medium", "high"})
#: Problem codes with which the gateway declines a change: answers, not failures
#: (``REQ-TGW-028``). The contract's closed set for a propose that is not possible.
REFUSAL_CODES: Final[frozenset[str]] = frozenset(
    {"not_found", "invalid_state", "not_permitted", "limit_exceeded"}
)
#: Problem codes with which the gateway answers a read that found nothing this customer
#: may see (``REQ-TGW-029``): an answer, not a failure, so a customer's wrong order
#: numbers do not open the breaker that every other customer's lookups share.
READ_REFUSAL_CODES: Final[frozenset[str]] = frozenset({"not_found", "not_permitted"})
#: Problem codes with which the gateway answers an execute that acted on nothing: the
#: proposal is unknown to it, or expired (``REQ-TGW-017``). ``already_executed`` is
#: deliberately absent: the change may have been made, so it stays *unknown*.
EXECUTE_NOT_DONE_CODES: Final[frozenset[str]] = frozenset({"not_found", "proposal_expired"})
_IDEMPOTENCY_HEADER: Final[str] = "Idempotency-Key"
_IDEMPOTENCY_PREFIX: Final[str] = "airo-"
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")

Kind = Literal["read", "write"]


def is_capability_name(value: str) -> bool:
    """Whether ``value`` is shaped like a capability name. A name a planner made up
    is model output; only a well-formed one is ever echoed into a prompt or a stage
    detail."""
    return len(value) <= _NAME_CHARS and _NAME.match(value) is not None


def idempotency_key(proposal_id: str) -> str:
    """The ``Idempotency-Key`` for executing this service's proposal ``proposal_id``:
    the same proposal always gives the same key (``REQ-TGW-015``)."""
    return f"{_IDEMPOTENCY_PREFIX}{proposal_id}"


class ToolsGatewayError(DownstreamError):
    """air-tools-gateway produced nothing usable. ``reason`` is one of spec 0010's four words."""


@dataclass(frozen=True, slots=True)
class Capability:
    """One checked inventory entry, with its argument schema compiled once."""

    name: str
    kind: Kind
    version: int
    description: str
    input_schema: Mapping[str, Any]
    requires_subject: bool
    risk: str | None
    _validator: Draft202012Validator = field(repr=False, compare=False)

    def argument_errors(self, arguments: Mapping[str, Any]) -> list[str]:
        """Why ``arguments`` do not fit this capability, or ``[]``. Each message is
        the failing path and validator keyword only, never the offending value: the
        value came from a model reading customer text."""
        return sorted(
            f"{'/'.join(str(p) for p in error.absolute_path) or '(root)'}: {error.validator}"
            for error in self._validator.iter_errors(dict(arguments))
        )


@dataclass(frozen=True, slots=True)
class Inventory:
    """One tenant's capabilities as last fetched. Empty is a value: no gateway, no
    keys for the tenant, or a failed fetch past its grace."""

    capabilities: tuple[Capability, ...] = ()
    #: ``False`` when the gateway could not be asked and no inventory inside its
    #: grace was left to serve: the turn is narrowed by an outage, which ``GATHER``
    #: reports as ``degraded``, unlike a tenant that simply has nothing on offer.
    available: bool = True

    def get(self, name: str) -> Capability | None:
        return next((c for c in self.capabilities if c.name == name), None)

    def for_channel(self, channel: Channel, settings: ToolsGatewaySettings) -> Inventory:
        """What ``channel`` may use: its allow-list (``REQ-TGW-004``), and no write on
        the business channel (``REQ-TGW-026``). ``/v1/capabilities`` reports from this
        (``REQ-TGW-005``); a turn narrows it further with :meth:`offered_to`."""
        return Inventory(
            available=self.available,
            capabilities=tuple(
                capability
                for capability in self.capabilities
                if settings.allow.admits(channel, capability.name)
                and not (capability.kind == "write" and channel is Channel.BUSINESS)
            ),
        )

    def offered_to(self, principal: Principal, settings: ToolsGatewaySettings) -> Inventory:
        """What this principal's turn may use: :meth:`for_channel`, less any capability
        that needs a customer when the turn has none."""
        channel = self.for_channel(principal.channel, settings)
        return Inventory(
            available=channel.available,
            capabilities=tuple(
                capability
                for capability in channel.capabilities
                if not (capability.requires_subject and principal.subject is None)
            ),
        )

    def has(self, kind: Kind) -> bool:
        return any(capability.kind == kind for capability in self.capabilities)


class ToolResult(BaseModel):
    """A lookup's answer, in the contract's ``ToolResult`` shape."""

    model_config = ConfigDict(extra="ignore")

    capability: str
    data: dict[str, Any] | list[Any]
    truncated: bool = False
    as_of: datetime


class GatewayProposal(BaseModel):
    """The gateway's ``ProposalResponse``. ``proposal_id`` stays server-side
    (``REQ-TGW-013``)."""

    model_config = ConfigDict(extra="ignore")

    proposal_id: str = Field(min_length=1, max_length=128)
    capability: str
    risk: Literal["low", "medium", "high"]
    summary: str = Field(min_length=1, max_length=500)
    expires_at: datetime
    preview: dict[str, Any] | None = None


class ExecuteOutcome(BaseModel):
    """The gateway's ``ExecuteResponse``. ``result`` is not kept: nothing here renders
    it, and the customer's answer is fixed text chosen by ``status`` and ``code``
    (``REQ-TGW-017``)."""

    model_config = ConfigDict(extra="ignore")

    status: Literal["done", "pending_approval", "rejected"]
    execution_id: str = Field(min_length=1, max_length=128)
    capability: str
    #: Present when ``status`` is ``rejected``: a contract ``ProblemCode``. Only read
    #: against a closed set of answers, never echoed.
    code: str | None = Field(default=None, max_length=64)


@dataclass
class _TenantCache:
    inventory: Inventory | None = None
    fetched_at: float = 0.0
    attempted_at: float | None = None
    reported: set[tuple[str, str]] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _printable(value: object) -> str:
    """An untrusted entry name, safe to put in a log line."""
    text = value if isinstance(value, str) else type(value).__name__
    return _CONTROL.sub("?", text)[:_NAME_CHARS] or "?"


def _check_entry(raw: object) -> tuple[Capability | None, str, str]:
    """``(capability, name, "")`` for a good entry, ``(None, name, reason)`` for a bad one."""
    if not isinstance(raw, dict):
        return None, "?", "not an object"
    name = raw.get("name")
    printable = _printable(name)
    if not isinstance(name, str) or len(name) > _NAME_CHARS or not _NAME.match(name):
        return None, printable, "name"
    kind = raw.get("kind")
    if kind not in ("read", "write"):
        return None, name, "kind"
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return None, name, "version"
    description = raw.get("description")
    if not isinstance(description, str) or not description or len(description) > _DESCRIPTION_CHARS:
        return None, name, "description"
    requires_subject = raw.get("requires_subject")
    if not isinstance(requires_subject, bool):
        return None, name, "requires_subject"
    risk = raw.get("risk")
    if kind == "write" and risk not in _RISKS:
        return None, name, "risk"
    schema = raw.get("input_schema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return None, name, "input_schema"
    if schema.get("additionalProperties") is not False or "patternProperties" in schema:
        return None, name, "input_schema"
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or _IDENTITY_FIELDS & set(properties):
        return None, name, "input_schema"
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        return None, name, "input_schema"
    return (
        Capability(
            name=name,
            kind=kind,
            version=version,
            description=description,
            input_schema=schema,
            requires_subject=requires_subject,
            risk=risk if kind == "write" else None,
            _validator=Draft202012Validator(schema),
        ),
        name,
        "",
    )


class ToolsGatewayClient(DownstreamClient):
    """The read-key half of air-tools-gateway. See the module docstring."""

    def __init__(
        self, config: ToolsGatewaySettings, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        super().__init__(DownstreamService.TOOLS_GATEWAY, config, clock=clock)
        self.settings = config
        self._clock = clock
        self._tenants: dict[str, _TenantCache] = {}

    async def inventory(self, tenant: str, *, timeout_s: float) -> Inventory:
        """``tenant``'s capabilities. Never raises: a failure narrows the turn
        (Article III, ``REQ-TGW-003``)."""
        keys = self.settings.keys_for(tenant)
        if keys is None:
            return Inventory()
        cache = self._tenants.setdefault(tenant, _TenantCache())
        async with cache.lock:
            now = self._clock()
            due = (
                cache.attempted_at is None
                or now - cache.attempted_at >= self.settings.inventory_ttl_s
            )
            if due:
                cache.attempted_at = now
                try:
                    raw = await self.get_json(
                        _INVENTORY_PATH,
                        timeout_s=timeout_s,
                        parse=_capability_list,
                        api_key=keys.read,
                    )
                except DownstreamError:
                    pass  # logged and counted by the base; the grace rule below decides
                else:
                    cache.inventory = self._checked(tenant, raw, cache)
                    cache.fetched_at = now
            if cache.inventory is None:
                return Inventory(available=False)
            if now - cache.fetched_at >= self.settings.inventory_grace_s:
                return Inventory(available=False)
            return cache.inventory

    async def run(
        self,
        capability: Capability,
        arguments: Mapping[str, Any],
        *,
        tenant: str,
        subject: str | None,
        timeout_s: float,
    ) -> ToolResult:
        """Run a ``read`` capability, or raise :class:`ToolsGatewayError`. The caller
        has already validated ``arguments`` (``REQ-TGW-006``). A ``not_found`` or
        ``not_permitted`` raises with ``reason="refused"`` and its ``code``: the gateway
        answering, which the breaker counts as a success (``REQ-TGW-029``)."""
        if capability.kind != "read":
            raise ValueError(f"{capability.name} is not a read capability")
        return await self._call(
            capability,
            f"/v1/tools/{capability.name}",
            arguments,
            tenant=tenant,
            subject=subject,
            timeout_s=timeout_s,
            parse=lambda body: _for(capability, ToolResult.model_validate(body)),
            refusal_codes=READ_REFUSAL_CODES,
        )

    async def propose(
        self,
        capability: Capability,
        arguments: Mapping[str, Any],
        *,
        tenant: str,
        subject: str | None,
        timeout_s: float,
    ) -> GatewayProposal:
        """Prepare a ``write`` capability; the gateway changes nothing. Raises
        :class:`ToolsGatewayError`: ``reason="refused"`` with a ``code`` from
        :data:`REFUSAL_CODES` when the gateway declines it, else a failure."""
        if capability.kind != "write":
            raise ValueError(f"{capability.name} is not a write capability")
        return await self._call(
            capability,
            f"/v1/actions/{capability.name}/propose",
            arguments,
            tenant=tenant,
            subject=subject,
            timeout_s=timeout_s,
            parse=lambda body: _for(capability, GatewayProposal.model_validate(body)),
            refusal_codes=REFUSAL_CODES,
        )

    async def _call[T: (ToolResult, GatewayProposal)](
        self,
        capability: Capability,
        path: str,
        arguments: Mapping[str, Any],
        *,
        tenant: str,
        subject: str | None,
        timeout_s: float,
        parse: Callable[[Any], T],
        refusal_codes: frozenset[str] = frozenset(),
    ) -> T:
        keys = self.settings.keys_for(tenant)
        if keys is None:
            raise ValueError("this tenant has no air-tools-gateway keys")
        if capability.requires_subject and subject is None:
            raise ValueError(f"{capability.name} needs a customer and the turn has none")
        body = {
            "arguments": dict(arguments),
            "subject": subject if capability.requires_subject else None,
        }
        try:
            result = await self.post_json(
                path,
                body,
                timeout_s=timeout_s,
                parse=parse,
                api_key=keys.read,
                refusal_codes=refusal_codes,
            )
        except DownstreamError as exc:
            metrics.record_capability_call(
                capability=capability.name, kind=capability.kind, outcome=exc.reason
            )
            raise
        metrics.record_capability_call(
            capability=capability.name, kind=capability.kind, outcome="ok"
        )
        return result

    def _checked(self, tenant: str, raw: list[Any], cache: _TenantCache) -> Inventory:
        kept: list[Capability] = []
        seen: set[str] = set()
        for entry in raw:
            capability, name, reason = _check_entry(entry)
            if capability is not None and capability.name in seen:
                capability, reason = None, "duplicate"
            if capability is not None and len(kept) >= MAX_CAPABILITIES:
                capability, reason = None, "over_limit"
            if capability is None:
                if (name, reason) not in cache.reported:
                    cache.reported.add((name, reason))
                    logger.warning(
                        "tools.inventory.entry_dropped",
                        tenant=tenant,
                        capability=name,
                        reason=reason,
                    )
                continue
            seen.add(capability.name)
            kept.append(capability)
        return Inventory(tuple(kept))

    def _error(self, message: str, *, reason: str, status: int | None) -> DownstreamError:
        return ToolsGatewayError(message, service=self.service, reason=reason, status=status)


class ActionExecutor(DownstreamClient):
    """The execute-key half of air-tools-gateway. Reached only from a confirmation
    turn (plan §2). See the module docstring."""

    def __init__(
        self, config: ToolsGatewaySettings, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        # No default key on the HTTP client: each request names the execute key.
        super().__init__(
            DownstreamService.TOOLS_GATEWAY,
            config.model_copy(update={"api_key": None}),
            clock=clock,
        )
        self.settings = config

    def bound_s(self, remaining_s: float | None, *, deadline_s: float | None = None) -> float:
        """``execute_timeout_ms``, narrowed only by what is left of the turn. Not by a
        deadline share: a confirmation turn calls nothing else, and a change cut
        short is an outcome the customer cannot be told (``REQ-TGW-017``)."""
        bound = self.settings.execute_timeout_ms / 1000
        return bound if remaining_s is None else min(bound, remaining_s)

    async def execute(
        self,
        capability: str,
        *,
        proposal_id: str,
        gateway_proposal_id: str,
        tenant: str,
        subject: str | None,
        timeout_s: float,
    ) -> ExecuteOutcome:
        """Execute a confirmed proposal, once, or raise :class:`ToolsGatewayError`.
        ``proposal_id`` is this service's id and gives the idempotency key;
        ``gateway_proposal_id`` is what the gateway knows it by. A ``404 not_found`` or
        ``409 proposal_expired`` raises with ``reason="refused"`` and its ``code``: the
        gateway answering that nothing was done, which does not count against the
        breaker."""
        if not is_capability_name(capability):
            raise ValueError("not a capability name")
        keys = self.settings.keys_for(tenant)
        if keys is None:
            raise ValueError("this tenant has no air-tools-gateway keys")
        try:
            outcome = await self.post_json(
                f"/v1/actions/{capability}/execute",
                {"proposal_id": gateway_proposal_id, "subject": subject},
                timeout_s=timeout_s,
                parse=lambda body: _executed(capability, body),
                api_key=keys.execute,
                headers={_IDEMPOTENCY_HEADER: idempotency_key(proposal_id)},
                refusal_codes=EXECUTE_NOT_DONE_CODES,
            )
        except DownstreamError as exc:
            metrics.record_capability_call(capability=capability, kind="write", outcome=exc.reason)
            raise
        metrics.record_capability_call(capability=capability, kind="write", outcome="ok")
        return outcome

    def _error(self, message: str, *, reason: str, status: int | None) -> DownstreamError:
        return ToolsGatewayError(message, service=self.service, reason=reason, status=status)


def _executed(capability: str, body: Any) -> ExecuteOutcome:
    outcome = ExecuteOutcome.model_validate(body)
    if outcome.capability != capability:
        raise ValueError("response names another capability")
    return outcome


def _capability_list(body: Any) -> list[Any]:
    """The inventory envelope, checked only as far as finding the list: entries are
    checked one by one, so one bad entry does not fail the whole response."""
    if not isinstance(body, dict) or not isinstance(body.get("capabilities"), list):
        raise ValueError("not an inventory")
    return list(body["capabilities"])


def _for[T: (ToolResult, GatewayProposal)](capability: Capability, result: T) -> T:
    """A response for another capability is not an answer to this call."""
    if result.capability != capability.name:
        raise ValueError("response names another capability")
    return result
