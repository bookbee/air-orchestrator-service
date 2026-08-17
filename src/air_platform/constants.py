"""Stable identifiers shared across the service.

Everything in this module is public surface: these values appear in API
responses, in Prometheus metric labels, and in configuration keys. Renaming a
member is a breaking change for callers and dashboards alike.

`Stage` and `Route` are deliberately *closed* sets. Both are used as Prometheus
label values, and an open-ended string would let label cardinality grow without
bound — the same rule air-classifier applies to `EscalationReason`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Channel(StrEnum):
    """Which gateway a turn arrived through.

    This is the platform's central distinction — see docs/01-hld.md §3. It is
    resolved from the authenticated principal, never from a request header: a
    client that could name its own channel could select the weaker guardrail
    profile, which is the whole thing the split exists to prevent.
    """

    CUSTOMER = "customer"
    BUSINESS = "business"


class Route(StrEnum):
    """What the planner may decide a turn needs.

    A route is *available* only when the service behind it is reachable and
    advertises the capability, so this enum is the vocabulary, not the inventory
    — `/v1/capabilities` reports which of these are live right now.
    """

    DIRECT = "direct"
    RAG = "rag"
    TOOLS = "tools"
    RECOMMEND = "recommend"
    ACTION = "action"


class Stage(StrEnum):
    """The turn pipeline's steps, in the order docs/01-hld.md §4 runs them.

    Emitted as `stage` events and as a metric label, so the order here is the
    order a client sees.
    """

    GUARDRAILS_IN = "guardrails_in"
    CONTEXT = "context"
    CACHE = "cache"
    CLASSIFY = "classify"
    PLAN = "plan"
    GATHER = "gather"
    SYNTHESISE = "synthesise"
    GUARDRAILS_OUT = "guardrails_out"
    PERSIST = "persist"


class StageStatus(StrEnum):
    """How a stage finished.

    `blocked` is distinct from `degraded` on purpose: a guardrail refusing a turn
    is the system working, while a downstream being unreachable is the system
    coping. Collapsing them would make a refusal spike look like an outage.
    """

    OK = "ok"
    SKIPPED = "skipped"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class TurnStatus(StrEnum):
    """A turn's terminal outcome, carried on `turn.end`.

    Note this is not the HTTP status. A streaming response commits to 200 before
    the pipeline runs, so this is the only honest report of what happened — which
    is why the access log records both (docs/02-lld.md §11).
    """

    OK = "ok"
    REFUSED = "refused"
    DEGRADED = "degraded"
    ERROR = "error"


class DownstreamService(StrEnum):
    """The AIR services a turn may call.

    Values match the repository names so that a metric label, a config key and a
    log line all say the same word.
    """

    INFRA = "air-infra"
    CLASSIFIER = "air-classifier"
    RAG = "air-rag"
    TOOLS = "air-tools"
    ACTION = "air-action"
    RECOMMENDER = "air-recommender"


# ── Scopes ────────────────────────────────────────────────────────────────────

#: Send a conversational turn on the customer channel.
SCOPE_CHAT_WRITE: Final[str] = "chat:write"
#: Send a business query on the business channel.
SCOPE_QUERY_WRITE: Final[str] = "query:write"
#: Read or delete a session this key owns.
SCOPE_SESSION_READ: Final[str] = "session:read"
#: Read capabilities and other operational surfaces.
SCOPE_ADMIN_READ: Final[str] = "admin:read"

#: Scopes each channel can meaningfully hold. Used at startup to reject a key
#: record that grants something its channel can never exercise — a misconfigured
#: key that 403s at request time is far harder to diagnose than one that refuses
#: to load.
CHANNEL_SCOPES: Final[dict[Channel, frozenset[str]]] = {
    Channel.CUSTOMER: frozenset({SCOPE_CHAT_WRITE, SCOPE_SESSION_READ, SCOPE_ADMIN_READ}),
    Channel.BUSINESS: frozenset({SCOPE_QUERY_WRITE, SCOPE_SESSION_READ, SCOPE_ADMIN_READ}),
}


# ── Headers and URIs ──────────────────────────────────────────────────────────

#: Header names, defined once so middleware and clients cannot drift apart.
HEADER_API_KEY: Final[str] = "X-API-Key"
HEADER_REQUEST_ID: Final[str] = "X-Request-ID"
HEADER_IDEMPOTENCY_KEY: Final[str] = "Idempotency-Key"

#: Base URI for RFC 9457 `type` values. Shared with the other AIR services so a
#: client parses one error namespace across the estate.
ERROR_TYPE_BASE: Final[str] = "https://air.dev/errors"

#: The v1 prefix, defined once so the router and the metric labels agree.
V1_PREFIX: Final[str] = "/v1"
