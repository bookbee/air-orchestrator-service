"""Application settings.

Configuration is 12-factor: every value below is settable from the environment
using the ``AIR_ORCHESTRATOR_SERVICE__`` prefix and ``__`` as the nesting delimiter, e.g.::

    AIR_ORCHESTRATOR_SERVICE__TURN__DEADLINE_MS=8000
    AIR_ORCHESTRATOR_SERVICE__DOWNSTREAM__RAG__ENABLED=false

The prefix and the nested ``app.env`` shape follow **air-classifier**, not
air-infra: air-infra is infrastructure and uses a top-level ``environment`` of
``local|staging|prod``, but air-orchestrator-service is a service, and matching the service
convention is what keeps a single ``air-client`` target block coherent across
the estate.

Turn budgets and guardrail profiles live here rather than in code for the same
reason air-classifier's escalation thresholds do: tuning the cost/latency
balance must never require deploying new logic, only new values.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from air_orchestrator_service.constants import Channel, DownstreamService, ModelClass, Route

# ── Application ───────────────────────────────────────────────────────────────


class AppSettings(BaseModel):
    """Process-level knobs."""

    env: Literal["development", "staging", "production", "test"] = "development"
    host: str = "0.0.0.0"  # noqa: S104 — containers bind all interfaces by design
    #: 8081 is this service's slot in the AIR port map (air-infra/README.md).
    #: 8080 is air-infra, the store/secrets broker this service calls for
    #: Redis/Postgres/Mongo credentials; 8083 is air-model-gateway.
    port: int = Field(default=8081, ge=1, le=65535)
    workers: int = Field(default=2, ge=1, le=64)
    root_path: str = ""
    docs_enabled: bool = True
    max_body_bytes: int = Field(default=1_048_576, ge=1024)
    shutdown_grace_seconds: float = Field(default=15.0, ge=0.0)

    @property
    def is_production(self) -> bool:
        return self.env == "production"


# ── air-infra ─────────────────────────────────────────────────────────────────


class InfraSettings(BaseModel):
    """The air-infra broker: Redis/Postgres/Mongo credentials and secrets.

    Not the model path — that is air-model-gateway (:class:`ModelGatewaySettings`),
    reached directly and never through air-infra (Article VII). Kept mandatory (no
    ``enabled`` flag, unlike :class:`DownstreamSettings`) because Phase 2's
    Redis-backed session store will depend on it; today nothing in this service
    actually calls it yet.
    """

    base_url: str = "http://localhost:8080"
    #: air-infra service token, sent as ``X-API-Key``.
    api_key: SecretStr | None = None
    timeout_s: float = Field(default=30.0, gt=0)
    #: Readiness probes must not inherit the generous turn timeout: a slow probe
    #: turns a health check into an outage detector that fires too late.
    health_timeout_s: float = Field(default=2.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=8)

    @property
    def configured(self) -> bool:
        return self.api_key is not None


# ── air-model-gateway ─────────────────────────────────────────────────────────


class ModelGatewaySettings(BaseModel):
    """air-model-gateway: the only model path (Article VII, amended 1.1.0).

    The one downstream whose absence is fatal to a turn — no synthesis is possible
    without it — so unlike the services in :class:`DownstreamSettings` it has no
    ``enabled`` flag. It is either serving this service or the service reports
    itself unready.

    ``model_class`` is a :class:`ModelClass`, never a model or alias name. The
    gateway owner assigned this service ``cloud`` when ``/v1/inference`` and its
    aliases were removed (air-model-gateway ``docs/06-consumer-migration.md``);
    a laptop with only Ollama sets ``local``. Typed as the enum so a leftover
    alias such as ``generative`` fails here, at boot, rather than as a 422 on
    every call while readiness reports green — which is exactly how the previous
    contract break went unnoticed.
    """

    base_url: str = "http://localhost:8083"
    #: This service's gateway token (``<service>-model-gateway-token``), sent as
    #: ``X-API-Key`` on every call — the readiness probe included, since the
    #: probe's job is to prove the token works.
    api_key: SecretStr | None = None
    model_class: ModelClass = ModelClass.CLOUD
    timeout_s: float = Field(default=30.0, gt=0)
    #: Readiness probes must not inherit the generous turn timeout: a slow probe
    #: turns a health check into an outage detector that fires too late.
    health_timeout_s: float = Field(default=2.0, gt=0)
    #: A streamed answer's limit on *silence* between frames, not its duration
    #: (the turn deadline bounds that). Above the gateway's own 30 s idle limit, so
    #: the gateway's `error` event — which says why — arrives before this fires.
    stream_idle_timeout_s: float = Field(default=45.0, gt=0)

    @property
    def configured(self) -> bool:
        return self.api_key is not None


# ── Downstream services ───────────────────────────────────────────────────────


class ServiceSettings(BaseModel):
    """One optional AIR service the planner may route to.

    ``enabled=false`` and "unreachable" are deliberately the same fact from two
    sides: both remove the capability from ``/v1/capabilities`` and from the
    planner's inventory. That is what makes most of the estate being unbuilt a
    supported deployment rather than a broken one.
    """

    enabled: bool = False
    base_url: str = ""
    api_key: SecretStr | None = None
    timeout_ms: int = Field(default=5000, ge=1, le=120_000)
    #: The most of the turn's whole deadline one call may use. A downstream call is
    #: enrichment; the answer is the product (spec 0005 ``REQ-CLS-014``, spec 0011
    #: ``REQ-RAG-004``).
    deadline_share: float = Field(default=0.25, gt=0, le=1)
    #: ``GET /v1/health`` on ``/v1/capabilities`` (spec 0010, ``REQ-CLB-007``).
    health_timeout_ms: int = Field(default=2000, ge=1, le=30_000)
    #: Consecutive failures that open this service's breaker, and how long it stays
    #: open before one call tests it (spec 0010, ``REQ-CLB-004``/``005``).
    breaker_threshold: int = Field(default=5, ge=1, le=1000)
    breaker_cooldown_s: float = Field(default=30.0, gt=0, le=3600)

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.base_url)


class ClassifierSettings(ServiceSettings):
    """air-classifier (spec 0005), with the two limits this service passes on."""

    #: The highest rung of air-classifier's ladder a call may reach
    #: (``REQ-CLS-016``). ``None`` sends nothing, and the classifier's own default
    #: applies. ``t2_local_llm`` keeps customer text on-premises and free.
    max_tier: Literal["t0_rules", "t1_classifier", "t2_local_llm", "t3_cloud_llm"] | None = None


class RagSettings(ServiceSettings):
    """air-rag-service (spec 0011): how much to ask for, and how much of it to use.

    air-rag-service has no minimum-score or latency option, so the filtering is done here.
    The defaults keep the sources block in the prompt to roughly 5k characters.
    """

    deadline_share: float = Field(default=0.2, gt=0, le=1)
    #: Passages to ask air-rag-service for. More than ``max_passages``, so the score filter
    #: has something to choose among.
    top_k: int = Field(default=6, ge=1, le=50)
    #: Passages that may reach the prompt (``REQ-RAG-005``).
    max_passages: int = Field(default=4, ge=1, le=20)
    #: air-rag-service's score is cosine similarity, clamped to 0..1. Measured on the SIT
    #: corpus with local-768-v1 (nomic-embed-text), relevant top hits scored 0.55 to 0.81
    #: and off-topic queries' top hits 0.39 to 0.48, so 0.5 separates them. One corpus
    #: and one model: re-measure when real documents or another embedding model land.
    min_score: float = Field(default=0.5, ge=0, le=1)
    #: Each passage's snippet is cut to this many characters.
    snippet_chars: int = Field(default=1200, ge=100, le=8000)
    #: This service's tenant -> the air-rag-service key for it (spec 0011, ``REQ-RAG-016``).
    #: air-rag-service searches only the tenant on the calling key's record, so this map is
    #: what keeps one tenant's documents out of another's turns. When it is set, a
    #: tenant missing from it does not retrieve (``REQ-RAG-017``). When it is empty,
    #: ``api_key`` serves every tenant: a single-tenant deployment. Set as JSON, e.g.
    #: ``{"tenant-a": "airr_..."}``.
    tenant_keys: dict[str, SecretStr] = Field(default_factory=dict)

    def key_for(self, tenant: str) -> SecretStr | None:
        """The air-rag-service key to call with for ``tenant``, or ``None`` when this tenant
        has no knowledge base. ``api_key`` only when no per-tenant keys are set."""
        if self.tenant_keys:
            return self.tenant_keys.get(tenant)
        return self.api_key

    def serves(self, tenant: str) -> bool:
        return not self.tenant_keys or tenant in self.tenant_keys


#: A capability name (spec 0012 ``REQ-TGW-002``), or a ``namespace.*`` pattern.
_CAPABILITY_NAME: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
_CAPABILITY_PATTERN: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*\.\*$")


class GatewayKeyPair(BaseModel):
    """One tenant's two air-tools-gateway keys (spec 0012 §9 Q2).

    Two keys rather than one with two scopes, because the split is what keeps the
    execute key out of reach of every request but an execute: the read key serves
    the inventory, lookups and proposals, and the execute key serves execute alone.
    """

    model_config = ConfigDict(extra="forbid")

    read: SecretStr
    execute: SecretStr


class ChannelAllowList(BaseModel):
    """Which capabilities each channel may use (spec 0012 ``REQ-TGW-004``, §9 Q4).

    Per channel, because Article I makes the channel decide the tool allow-list.
    Empty by default: a capability the gateway advertises is usable only once a
    deployment has said so. A pattern ``orders.*`` admits every capability whose
    name starts ``orders.``, so a capability added there needs no change here
    (``REQ-TGW-001``).
    """

    model_config = ConfigDict(extra="forbid")

    customer: list[str] = Field(default_factory=list)
    business: list[str] = Field(default_factory=list)

    @field_validator("customer", "business")
    @classmethod
    def _valid_entries(cls, entries: list[str]) -> list[str]:
        for entry in entries:
            if not (_CAPABILITY_NAME.match(entry) or _CAPABILITY_PATTERN.match(entry)):
                raise ValueError(
                    f"{entry!r} is neither a capability name nor a 'namespace.*' pattern"
                )
        return entries

    def admits(self, channel: Channel, capability: str) -> bool:
        entries = self.customer if channel is Channel.CUSTOMER else self.business
        for entry in entries:
            if entry.endswith(".*"):
                if capability.startswith(entry[:-1]):
                    return True
            elif entry == capability:
                return True
        return False


class ToolsGatewaySettings(ServiceSettings):
    """air-tools-gateway (spec 0012): lookups and proposed changes, one service.

    ``api_key`` / ``execute_key`` are a single-tenant pair, used only while
    ``tenant_keys`` is empty, as ``RagSettings.api_key`` is (``REQ-TGW-007``).
    """

    timeout_ms: int = Field(default=3000, ge=100, le=30_000)
    #: Execute talks to a system of record and is never retried
    #: (``REQ-TGW-018``), so it gets more room than a lookup.
    execute_timeout_ms: int = Field(default=8000, ge=100, le=30_000)
    #: Lookups run alongside retrieval (0.2), not after it.
    deadline_share: float = Field(default=0.3, gt=0, le=1)
    execute_key: SecretStr | None = None
    #: This service's tenant -> its gateway key pair. When set, a tenant missing
    #: from it gets no capabilities. Set as JSON, e.g.
    #: ``{"brightcart": {"read": "airtg_...", "execute": "airtg_..."}}``.
    tenant_keys: dict[str, GatewayKeyPair] = Field(default_factory=dict)
    #: How often a tenant's inventory is refetched, and how long the last good one
    #: survives failed refreshes (``REQ-TGW-003``, §9 Q3).
    inventory_ttl_s: int = Field(default=60, ge=5, le=3600)
    inventory_grace_s: int = Field(default=300, ge=5, le=3600)
    #: Bound on one lookup result in the prompt (``REQ-TGW-009``).
    max_result_chars: int = Field(default=4000, ge=200, le=20_000)
    allow: ChannelAllowList = Field(default_factory=ChannelAllowList)

    @model_validator(mode="after")
    def _grace_covers_the_ttl(self) -> Self:
        if self.inventory_grace_s < self.inventory_ttl_s:
            raise ValueError("inventory_grace_s must be at least inventory_ttl_s")
        return self

    def keys_for(self, tenant: str) -> GatewayKeyPair | None:
        """``tenant``'s key pair, or ``None`` when it has no capabilities."""
        if self.tenant_keys:
            return self.tenant_keys.get(tenant)
        if self.api_key is None or self.execute_key is None:
            return None
        return GatewayKeyPair(read=self.api_key, execute=self.execute_key)

    def serves(self, tenant: str) -> bool:
        return self.keys_for(tenant) is not None


#: Sections of :class:`DownstreamSettings` that were removed, and what replaced them.
_RETIRED_DOWNSTREAMS: dict[str, str] = {
    "tools": "tools_gateway",
    "action": "tools_gateway",
}


class DownstreamSettings(BaseModel):
    """Defaults are *off*: most of these services are not deployed everywhere.

    A default of ``enabled=true`` would make a fresh checkout report itself
    degraded against services that do not exist, which trains an operator to
    ignore the degraded signal.
    """

    classifier: ClassifierSettings = Field(
        default_factory=lambda: ClassifierSettings(base_url="http://localhost:8082")
    )
    rag: RagSettings = Field(default_factory=lambda: RagSettings(base_url="http://localhost:8087"))
    tools_gateway: ToolsGatewaySettings = Field(
        default_factory=lambda: ToolsGatewaySettings(base_url="http://localhost:8084")
    )
    recommender: ServiceSettings = Field(
        default_factory=lambda: ServiceSettings(base_url="http://localhost:8086")
    )
    #: Tombstones for the retired air-tools and air-action sections (spec 0012).
    #: Typed loosely and excluded from dumps. They exist so that a deployment still
    #: setting ``DOWNSTREAM__TOOLS__*`` or ``DOWNSTREAM__ACTION__*`` is *seen*
    #: rather than silently dropped: ignored with a startup warning while disabled,
    #: refused when enabled (:meth:`_refuse_enabled_retired`).
    tools: dict[str, Any] | None = Field(default=None, exclude=True, repr=False)
    action: dict[str, Any] | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def _refuse_enabled_retired(self) -> Self:
        """Every ``.env`` made by an older ``make env`` sets these sections with
        ``ENABLED=false``, so refusing them outright would break every checkout.
        One that *enables* either meant something by it, and booting without the
        service it asked for would be the quieter failure."""
        for name, replacement in _RETIRED_DOWNSTREAMS.items():
            section = getattr(self, name) or {}
            if str(section.get("enabled", "false")).strip().lower() in {"true", "1", "yes", "on"}:
                raise ValueError(
                    f"the 'downstream.{name}' section was retired with spec 0012: "
                    f"air-tools-gateway now serves it; set "
                    f"AIR_ORCHESTRATOR_SERVICE__DOWNSTREAM__{replacement.upper()}__* instead"
                )
        return self

    def retired_sections(self) -> list[str]:
        """Retired sections present in the configuration, for a startup warning."""
        return [name for name in _RETIRED_DOWNSTREAMS if getattr(self, name) is not None]

    def for_service(self, service: DownstreamService) -> ServiceSettings | None:
        """Settings for a service, or ``None`` for air-infra/air-model-gateway.

        Those two are mandatory dependencies configured on their own settings
        objects (:class:`InfraSettings`, :class:`ModelGatewaySettings`), not this optional
        group.
        """
        attr = _SERVICE_ATTR.get(service)
        if attr is None:
            return None
        value = getattr(self, attr)
        assert isinstance(value, ServiceSettings)
        return value

    def routes(self) -> frozenset[Route]:
        """Routes whose backing service is configured.

        The planner's real inventory also depends on live capability discovery
        (Phase 3); this is the configuration half, and it is what
        ``/v1/capabilities`` can report before any probe has run.
        """
        live: set[Route] = {Route.DIRECT}  # always available: the model gateway alone answers it
        if self.rag.configured:
            live.add(Route.RAG)
        if self.tools_gateway.configured:
            # The configuration half. Which of the two a caller is offered depends
            # on the gateway's live inventory for that caller's channel
            # (spec 0012, ``REQ-TGW-005``).
            live.add(Route.TOOLS)
            live.add(Route.ACTION)
        if self.recommender.configured:
            live.add(Route.RECOMMEND)
        return frozenset(live)


#: Service → attribute name on :class:`DownstreamSettings`. air-infra and the model gateway
#: are absent deliberately: neither is optional, and neither lives in this group.
_SERVICE_ATTR: dict[DownstreamService, str] = {
    DownstreamService.CLASSIFIER: "classifier",
    DownstreamService.RAG: "rag",
    DownstreamService.TOOLS_GATEWAY: "tools_gateway",
    DownstreamService.RECOMMENDER: "recommender",
}


# ── Turn budget ───────────────────────────────────────────────────────────────


class CostEstimateSettings(BaseModel):
    """The rates a model call is priced at *before* it is made (spec 0008).

    air-model-gateway owns the price table and publishes none of it, and it takes
    no per-call cost cap, so the turn ceiling can only be enforced here from an
    estimate. The estimate must be an upper bound: an over-estimate stops a turn
    early, while an under-estimate overspends silently. Hence the defaults are list
    prices of the dearest model the ``cloud`` class reaches (claude-sonnet-5 in the
    gateway's ``deploy/pricing.yaml``), and three characters per token, fewer than
    typical English text, so the prompt is counted as more tokens than it is. A
    deployment that routes to a dearer model must raise them.
    """

    input_usd_per_mtok: float = Field(default=3.0, ge=0)
    output_usd_per_mtok: float = Field(default=15.0, ge=0)
    chars_per_token: float = Field(default=3.0, gt=0)


class TurnSettings(BaseModel):
    """Ceilings applied to a single turn.

    Every one of these is a *ceiling*, not a target: a per-request option may ask
    for less and never for more (docs/02-lld.md §5), so a caller cannot spend the
    operator's budget by asking nicely.
    """

    deadline_ms: int = Field(default=15_000, ge=100, le=300_000)
    max_cost_usd: float = Field(default=0.25, gt=0)
    estimate: CostEstimateSettings = Field(default_factory=CostEstimateSettings)
    max_model_calls: int = Field(default=4, ge=1, le=32)
    #: Turns of history assembled into the prompt. Older turns are summarised
    #: (Phase 2 ships truncation — docs/02-lld.md §15).
    window_turns: int = Field(default=12, ge=1, le=200)


# ── Guardrails ────────────────────────────────────────────────────────────────


class GuardrailProfile(BaseModel):
    """Which rules run, and in which direction, for one channel."""

    injection: bool = True
    #: Redaction on the business channel is configurable because internal
    #: analysts legitimately query customer records — configurable, never absent.
    redact_pii: bool = True
    grounding: bool = True
    #: Business-channel structured-output validation. Meaningless on the
    #: customer channel, which returns prose.
    validate_schema: bool = False


class GuardrailSettings(BaseModel):
    customer: GuardrailProfile = Field(default_factory=GuardrailProfile)
    business: GuardrailProfile = Field(
        default_factory=lambda: GuardrailProfile(validate_schema=True)
    )
    #: Names the scope guard treats as a competitor mention (guardrails/scope.py).
    #: Empty by default: air-orchestrator-service has no fixed vertical to hardcode names
    #: for, so this is a deployment's own configuration, not a code change.
    competitor_names: list[str] = Field(default_factory=list)

    def for_channel(self, channel: Channel) -> GuardrailProfile:
        return self.customer if channel is Channel.CUSTOMER else self.business


# ── Session and cache ─────────────────────────────────────────────────────────


class SessionSettings(BaseModel):
    """Conversation state. Redis keeps the orchestrator stateless and autoscalable."""

    backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/1"
    key_prefix: str = "air:plat:"
    ttl_seconds: int = Field(default=86_400, ge=60)
    #: A pending mutation stays confirmable only briefly, so a stale "yes" cannot
    #: execute an hour-old proposal (docs/00-plan.md §4 Q3).
    proposal_ttl_seconds: int = Field(default=300, ge=10, le=3600)
    #: Cumulative spend ceiling across a whole conversation, distinct from
    #: `TurnSettings.max_cost_usd`'s per-turn one. `None` (the default) is
    #: unlimited — opt-in, so a deployment that never sets this sees no
    #: behaviour change.
    max_cost_usd: float | None = Field(default=None, gt=0)


class CacheSettings(BaseModel):
    """Semantic cache. Off until Phase 5, and eligibility-gated when it lands.

    ``similarity_threshold`` is high by default on purpose: "where is order 123"
    and "where is order 456" are near-identical vectors, and a permissive
    threshold turns a cost win into a wrong-answer incident (docs/00-plan.md §4 Q5).
    """

    enabled: bool = False
    backend: Literal["memory", "redis"] = "memory"
    redis_url: str = "redis://localhost:6379/2"
    similarity_threshold: float = Field(default=0.97, ge=0.0, le=1.0)
    ttl_seconds: int = Field(default=3600, ge=0)
    max_entries: int = Field(default=10_000, ge=1)


# ── Prompts ───────────────────────────────────────────────────────────────────


#: A persona value is rendered into a system prompt, right above the ``<data>`` boundary
#: that wraps customer text — so it is one short plain line, with nothing that could
#: open a tag or a template field (spec 0006, ``REQ-PRM-004``).
_PERSONA_VALUE = re.compile(r"^[^<>{}\r\n]{1,60}$")


class PersonaSettings(BaseModel):
    """Who the assistant speaks as (spec 0006). The prompt's *rules* stay in code,
    versioned and reviewed; only the names are deployment settings."""

    brand_name: str = "AIR"
    #: Unset: "a customer care assistant". Set: "<name>, a customer care assistant".
    agent_name: str | None = None

    @field_validator("brand_name", "agent_name")
    @classmethod
    def _one_plain_line(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not _PERSONA_VALUE.match(value):
            raise ValueError("must be 1-60 characters on one line, without < > { }")
        return value


class PromptSettings(BaseModel):
    """Versioned prompts. A prompt is production logic, so it is pinned."""

    registry_path: Path | None = None
    #: prompt name → pinned version, e.g. ``{"customer_care": "v1"}``. Checked at
    #: startup (``PromptRegistry.validate_pins``), so a stale pin fails the boot.
    pins: dict[str, str] = Field(default_factory=dict)
    persona: PersonaSettings = Field(default_factory=PersonaSettings)


# ── Security ──────────────────────────────────────────────────────────────────


class SecuritySettings(BaseModel):
    #: JSON array of key records; see security/api_keys.py for the shape.
    api_keys_inline: str | None = None
    api_keys_file: Path | None = None
    #: Development escape hatch. Refused at startup when env == production.
    allow_unauthenticated: bool = False
    #: Channel granted to the anonymous identity when the hatch is open. Customer
    #: is the stricter profile, so an unauthenticated developer gets the tighter
    #: guardrails rather than the looser ones.
    anonymous_channel: Channel = Channel.CUSTOMER
    hash_salt: SecretStr = SecretStr("air-orchestrator-service-dev-salt")
    default_rate_limit_rpm: int = Field(default=120, ge=1)
    default_burst: int = Field(default=20, ge=1)
    #: Deliberate opt-in. When false, only a hash and length of user text is logged.
    log_raw_text: bool = False


# ── Observability ─────────────────────────────────────────────────────────────


class ObservabilitySettings(BaseModel):
    service_name: str = "air-orchestrator-service"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    otel_enabled: bool = False
    otel_endpoint: str | None = None
    otel_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)


# ── Root ──────────────────────────────────────────────────────────────────────


class Settings(BaseSettings):
    """Root settings object. Obtain via :func:`get_settings`."""

    model_config = SettingsConfigDict(
        env_prefix="AIR_ORCHESTRATOR_SERVICE__",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # A validation error is printed at boot, into the pod log. Without this,
        # pydantic renders the offending input verbatim — and the input to this
        # model is where every secret lives (gateway tokens, the hash salt, inline
        # key records). The message still names the field and the reason (Article IX).
        hide_input_in_errors=True,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    infra: InfraSettings = Field(default_factory=InfraSettings)
    model_gateway: ModelGatewaySettings = Field(default_factory=ModelGatewaySettings)
    #: Tombstone for the retired ``llm`` section — see :meth:`_refuse_retired_llm`.
    #: Typed loosely and excluded from dumps: it exists only so that a deployment
    #: still setting ``AIR_ORCHESTRATOR_SERVICE__LLM__*`` is *seen* rather than
    #: silently dropped by ``extra="ignore"``.
    llm: dict[str, Any] | None = Field(default=None, exclude=True, repr=False)
    downstream: DownstreamSettings = Field(default_factory=DownstreamSettings)
    turn: TurnSettings = Field(default_factory=TurnSettings)
    guardrails: GuardrailSettings = Field(default_factory=GuardrailSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    prompts: PromptSettings = Field(default_factory=PromptSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    obs: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    @model_validator(mode="after")
    def _refuse_retired_llm(self) -> Self:
        """Fail startup on the retired ``llm`` section rather than ignore it.

        Ignoring it would boot a service with no gateway token, which readiness
        then reports as a rejected token — correct, but a step removed from the
        cause. Two accepted names for one secret would be worse: a rotation that
        updates one and not the other. Article III allows exactly this kind of
        startup failure: settings that do not validate.
        """
        if self.llm is not None:
            raise ValueError(
                "the 'llm' settings section was retired with air-llm: rename "
                "AIR_ORCHESTRATOR_SERVICE__LLM__* to AIR_ORCHESTRATOR_SERVICE__MODEL_GATEWAY__* "
                "(and replace any model alias with MODEL_CLASS=local|auto|cloud)"
            )
        return self

    @model_validator(mode="after")
    def _guard_production(self) -> Self:
        """Fail fast rather than run production with a development hatch open."""
        if self.app.is_production:
            if self.security.allow_unauthenticated:
                raise ValueError(
                    "security.allow_unauthenticated must be false when app.env is production"
                )
            if self.security.log_raw_text:
                raise ValueError("security.log_raw_text must be false when app.env is production")
        return self

    @model_validator(mode="after")
    def _guard_redis_backends(self) -> Self:
        """A Redis-backed session store outside development needs a real URL.

        Sessions silently falling back to in-process memory is the failure that
        looks like it works: every replica answers, and a conversation simply
        forgets itself whenever the load balancer moves.
        """
        if self.session.backend == "redis" and not self.session.redis_url:
            raise ValueError("session.redis_url is required when session.backend is redis")
        if self.cache.enabled and self.cache.backend == "redis" and not self.cache.redis_url:
            raise ValueError("cache.redis_url is required when cache.backend is redis")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that import-time consumers and request-time consumers agree. Tests
    that mutate the environment should call ``get_settings.cache_clear()``.
    """
    return Settings()
