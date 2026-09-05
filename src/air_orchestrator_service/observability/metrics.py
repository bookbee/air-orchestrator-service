"""Prometheus metrics — docs/02-lld.md §13.

Every label value in this module comes from a closed enum or a config-declared
identity (a key id, a tenant, a service name). Nothing labels on a caller-supplied
string: an unbounded label value is how a 404 flood or a hostile session id turns
into a Prometheus outage.

``tenant`` is the one label that grows with the business rather than with the
code. It is included on cost only, where per-tenant attribution is the whole
point, and deliberately left off the high-frequency counters.
"""

from __future__ import annotations

from typing import Final

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

from air_orchestrator_service.constants import (
    Channel,
    DownstreamService,
    EscalationReason,
    Route,
    Stage,
    StageStatus,
    TurnStatus,
)

__all__ = [
    "CONTENT_TYPE",
    "REGISTRY",
    "record_downstream_call",
    "record_escalation",
    "record_guardrail_block",
    "record_request",
    "record_stage",
    "record_turn",
    "render",
]

#: A private registry rather than the global default. Two of the AIR services run
#: in one process during integration tests, and the global registry would make
#: the second one's collector registration raise on a duplicate name.
REGISTRY: Final[CollectorRegistry] = CollectorRegistry()

CONTENT_TYPE: Final[str] = "text/plain; version=0.0.4; charset=utf-8"

#: Latency buckets tuned to this service's shape, not the client default. A turn
#: that retrieves and calls tools lives in the 1-10s range, and the default
#: buckets stop at 10s with nothing useful in between.
_TURN_BUCKETS: Final[tuple[float, ...]] = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 6.0, 10.0, 20.0, 60.0)
_STAGE_BUCKETS: Final[tuple[float, ...]] = (0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 3.0, 10.0)


_REQUESTS = Counter(
    "air_orchestrator_service_requests_total",
    "HTTP requests, by route template and status.",
    ["endpoint", "status", "key_id"],
    registry=REGISTRY,
)

_REQUEST_SECONDS = Histogram(
    "air_orchestrator_service_request_seconds",
    "HTTP request duration.",
    ["endpoint"],
    buckets=_TURN_BUCKETS,
    registry=REGISTRY,
)

_TURNS = Counter(
    "air_orchestrator_service_turns_total",
    "Completed turns, by channel, terminal status and the primary route taken.",
    ["channel", "status", "route"],
    registry=REGISTRY,
)

_TURN_SECONDS = Histogram(
    "air_orchestrator_service_turn_seconds",
    "End-to-end turn duration.",
    ["channel"],
    buckets=_TURN_BUCKETS,
    registry=REGISTRY,
)

_STAGE_SECONDS = Histogram(
    "air_orchestrator_service_stage_seconds",
    "Per-stage duration within a turn.",
    ["stage", "status"],
    buckets=_STAGE_BUCKETS,
    registry=REGISTRY,
)

_TURN_COST = Counter(
    "air_orchestrator_service_turn_cost_usd_total",
    "Model spend attributed to turns. Read back from air-infra's accounting.",
    ["channel", "tenant"],
    registry=REGISTRY,
)

_CACHE_LOOKUPS = Counter(
    "air_orchestrator_service_cache_lookups_total",
    "Semantic cache lookups, by outcome.",
    ["result"],
    registry=REGISTRY,
)

_GUARDRAIL_BLOCKS = Counter(
    "air_orchestrator_service_guardrail_blocks_total",
    "Turns stopped by a guardrail, by direction and the rule that fired.",
    ["direction", "rule", "channel"],
    registry=REGISTRY,
)

_DOWNSTREAM_CALLS = Counter(
    "air_orchestrator_service_downstream_calls_total",
    "Calls to other AIR services, by outcome.",
    ["service", "outcome"],
    registry=REGISTRY,
)

_PROPOSALS = Counter(
    "air_orchestrator_service_proposals_total",
    "Mutation proposals, by what became of them.",
    ["outcome"],
    registry=REGISTRY,
)

_ESCALATIONS = Counter(
    "air_orchestrator_service_escalations_total",
    "Turns handed off to a human, by reason.",
    ["reason"],
    registry=REGISTRY,
)


def record_request(*, endpoint: str, status: int, key_id: str, duration_s: float) -> None:
    """One HTTP request. ``endpoint`` must be a route template, never a live path."""
    _REQUESTS.labels(endpoint, str(status), key_id).inc()
    _REQUEST_SECONDS.labels(endpoint).observe(duration_s)


def record_turn(
    *,
    channel: Channel,
    status: TurnStatus,
    route: Route,
    tenant: str,
    duration_s: float,
    cost_usd: float,
) -> None:
    """One completed turn.

    ``status`` is the turn's own outcome, not the HTTP status — a streaming
    response has already committed to 200 by the time this is known.
    """
    _TURNS.labels(channel.value, status.value, route.value).inc()
    _TURN_SECONDS.labels(channel.value).observe(duration_s)
    if cost_usd:
        _TURN_COST.labels(channel.value, tenant).inc(cost_usd)


def record_stage(*, stage: Stage, status: StageStatus, duration_s: float) -> None:
    _STAGE_SECONDS.labels(stage.value, status.value).observe(duration_s)


def record_cache_lookup(*, result: str) -> None:
    """``hit`` | ``miss`` | ``ineligible`` | ``error``.

    ``ineligible`` is separate from ``miss`` because they mean opposite things
    operationally: a miss argues for a longer TTL, while an ineligible lookup is
    the eligibility gate doing its job and must not be tuned away.
    """
    _CACHE_LOOKUPS.labels(result).inc()


def record_guardrail_block(*, direction: str, rule: str, channel: Channel) -> None:
    _GUARDRAIL_BLOCKS.labels(direction, rule, channel.value).inc()


def record_downstream_call(*, service: DownstreamService, outcome: str) -> None:
    """``ok`` | ``timeout`` | ``error`` | ``breaker_open`` | ``unavailable``."""
    _DOWNSTREAM_CALLS.labels(service.value, outcome).inc()


def record_proposal(*, outcome: str) -> None:
    """``created`` | ``confirmed`` | ``rejected`` | ``expired`` | ``cancelled``."""
    _PROPOSALS.labels(outcome).inc()


def record_escalation(*, reason: EscalationReason) -> None:
    _ESCALATIONS.labels(reason.value).inc()


def render() -> bytes:
    """The exposition payload for ``/metrics``."""
    return generate_latest(REGISTRY)
