"""Cross-cutting models: who is calling, and what the service can currently do."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from air_platform.constants import Channel, DownstreamService, Route


class Principal(BaseModel):
    """The authenticated caller.

    ``channel`` comes from the API key record, never from a header — see
    :class:`~air_platform.constants.Channel`. Everything downstream reads the
    channel from here, so there is exactly one place it can be decided.
    """

    model_config = ConfigDict(frozen=True)

    key_id: str = Field(description="Stable, non-secret; safe to log and to use as a metric label.")
    name: str
    channel: Channel
    tenant: str = Field(
        description=(
            "Isolation boundary for sessions, cache entries and downstream calls. "
            "Part of every storage key the service writes."
        )
    )
    scopes: frozenset[str] = frozenset()

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


class Usage(BaseModel):
    """What a turn consumed. Costs are read back from air-infra's own accounting
    rather than recomputed here, so the two can never disagree."""

    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    cache_hit: bool = False


# ── System surfaces ───────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    """Liveness. Deliberately dependency-free.

    A liveness probe that consults a downstream is a restart loop waiting to
    happen: air-infra having a bad minute would make every replica of this
    service look dead and get killed, which is precisely when you need them up.
    """

    status: Literal["ok"] = "ok"
    service: str
    version: str


class DependencyStatus(BaseModel):
    """One dependency's contribution to readiness."""

    service: DownstreamService
    configured: bool
    reachable: bool | None = Field(
        default=None,
        description="None when not probed — either unconfigured, or the probe is not run here.",
    )
    latency_ms: float | None = None
    detail: str | None = Field(
        default=None,
        description="Operator-facing reason. Never carries a URL, key or upstream body.",
    )


class ReadyResponse(BaseModel):
    """Readiness: can this replica serve a turn at all?

    ``ready`` tracks air-infra alone. Every other service being down is a
    narrower answer, not an unservable one (docs/01-hld.md §7), so listing them
    here is reporting, not gating — and conflating the two would take the
    service out of rotation for a degradation it is designed to absorb.
    """

    ready: bool
    service: str
    version: str
    checked_at: datetime
    dependencies: list[DependencyStatus] = Field(default_factory=list)


class CapabilitiesResponse(BaseModel):
    """What this deployment can actually do, for the caller in front of it.

    Reported per principal: a customer key and a business key see different
    routes and a different guardrail profile from the same process, and a QA
    result only means something next to the capabilities that produced it.
    """

    service: str
    version: str
    env: str
    channel: Channel
    routes: list[Route] = Field(description="Routes whose backing service is configured.")
    guardrails: dict[str, bool] = Field(description="Rules active for this caller's channel.")
    streaming: dict[str, bool] = Field(
        description=(
            "Which stream shapes this build emits. `token_deltas` is false until "
            "air-infra grows a streaming endpoint (docs/01-hld.md §5, §9)."
        )
    )
    session: dict[str, str | int] = Field(description="Backend and TTLs in force.")
    turn: dict[str, float | int] = Field(description="Ceilings applied to every turn.")
