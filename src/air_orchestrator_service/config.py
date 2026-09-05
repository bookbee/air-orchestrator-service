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

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from air_orchestrator_service.constants import Channel, DownstreamService, Route

# ── Application ───────────────────────────────────────────────────────────────


class AppSettings(BaseModel):
    """Process-level knobs."""

    env: Literal["development", "staging", "production", "test"] = "development"
    host: str = "0.0.0.0"  # noqa: S104 — containers bind all interfaces by design
    #: 8081 is this service's slot in the AIR port map (air-infra/README.md).
    #: 8080 is air-infra, the store/secrets broker this service calls for
    #: Redis/Postgres/Mongo credentials; 8083 is air-llm, the model gateway.
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

    Not the model path — that moved to air-llm (:class:`LlmSettings`) when air-llm
    was split out of air-infra. Kept mandatory (no ``enabled`` flag, unlike
    :class:`DownstreamSettings`) because Phase 2's Redis-backed session store will
    depend on it; today nothing in this service actually calls it yet.
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


# ── air-llm ───────────────────────────────────────────────────────────────────


class LlmSettings(BaseModel):
    """The air-llm gateway: the only model path (docs/01-hld.md §7, as amended).

    This is the one downstream whose absence is fatal to a turn — no synthesis is
    possible without it — so unlike the services in :class:`DownstreamSettings` it
    has no ``enabled`` flag. It is either reachable or the service reports itself
    unready.
    """

    base_url: str = "http://localhost:8083"
    #: air-llm service token, sent as ``X-API-Key``.
    api_key: SecretStr | None = None
    timeout_s: float = Field(default=30.0, gt=0)
    #: Readiness probes must not inherit the generous turn timeout: a slow probe
    #: turns a health check into an outage detector that fires too late.
    health_timeout_s: float = Field(default=2.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=8)
    #: Model/routing alias asked of air-llm. ``None`` takes air-llm's own default,
    #: which is the right choice locally where only Ollama is configured.
    default_model: str | None = None

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

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.base_url)


class DownstreamSettings(BaseModel):
    """Defaults are *off*: air-rag, air-tools and air-action are empty repos.

    A default of ``enabled=true`` would make a fresh checkout report itself
    degraded against services that do not exist, which trains an operator to
    ignore the degraded signal.
    """

    classifier: ServiceSettings = Field(
        default_factory=lambda: ServiceSettings(base_url="http://localhost:8082")
    )
    rag: ServiceSettings = Field(
        default_factory=lambda: ServiceSettings(base_url="http://localhost:8087")
    )
    tools: ServiceSettings = Field(
        default_factory=lambda: ServiceSettings(base_url="http://localhost:8084")
    )
    action: ServiceSettings = Field(
        default_factory=lambda: ServiceSettings(base_url="http://localhost:8085")
    )
    recommender: ServiceSettings = Field(
        default_factory=lambda: ServiceSettings(base_url="http://localhost:8086")
    )

    def for_service(self, service: DownstreamService) -> ServiceSettings | None:
        """Settings for a service, or ``None`` for air-infra/air-llm.

        Those two are mandatory dependencies configured on their own settings
        objects (:class:`InfraSettings`, :class:`LlmSettings`), not this optional
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
        live: set[Route] = {Route.DIRECT}  # always available: air-llm alone answers it
        if self.rag.configured:
            live.add(Route.RAG)
        if self.tools.configured:
            live.add(Route.TOOLS)
        if self.recommender.configured:
            live.add(Route.RECOMMEND)
        if self.action.configured:
            live.add(Route.ACTION)
        return frozenset(live)


#: Service → attribute name on :class:`DownstreamSettings`. air-infra and air-llm
#: are absent deliberately: neither is optional, and neither lives in this group.
_SERVICE_ATTR: dict[DownstreamService, str] = {
    DownstreamService.CLASSIFIER: "classifier",
    DownstreamService.RAG: "rag",
    DownstreamService.TOOLS: "tools",
    DownstreamService.ACTION: "action",
    DownstreamService.RECOMMENDER: "recommender",
}


# ── Turn budget ───────────────────────────────────────────────────────────────


class TurnSettings(BaseModel):
    """Ceilings applied to a single turn.

    Every one of these is a *ceiling*, not a target: a per-request option may ask
    for less and never for more (docs/02-lld.md §5), so a caller cannot spend the
    operator's budget by asking nicely.
    """

    deadline_ms: int = Field(default=15_000, ge=100, le=300_000)
    max_cost_usd: float = Field(default=0.25, gt=0)
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


class PromptSettings(BaseModel):
    """Versioned prompts. A prompt is production logic, so it is pinned."""

    registry_path: Path | None = None
    #: route name → pinned prompt version, e.g. ``{"synthesise": "v3"}``.
    pins: dict[str, str] = Field(default_factory=dict)


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
    )

    app: AppSettings = Field(default_factory=AppSettings)
    infra: InfraSettings = Field(default_factory=InfraSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    downstream: DownstreamSettings = Field(default_factory=DownstreamSettings)
    turn: TurnSettings = Field(default_factory=TurnSettings)
    guardrails: GuardrailSettings = Field(default_factory=GuardrailSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    prompts: PromptSettings = Field(default_factory=PromptSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    obs: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

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
