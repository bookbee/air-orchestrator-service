"""Operational surfaces: liveness, readiness, capabilities, metrics.

The three probes answer deliberately different questions, and conflating any two
of them is the classic way to build a service that either restarts under load or
stays in rotation while broken:

* ``/v1/health`` — is this process alive? Dependency-free, so a downstream having a
  bad minute cannot make every replica look dead and get killed.
* ``/v1/ready`` — can this replica serve a turn *at all*? Gates on air-llm alone —
  the model gateway, and the one dependency without which no answer can be
  synthesised — because every other service being down narrows the answer rather
  than preventing one (docs/01-hld.md §7). air-infra is reported alongside it for
  operator visibility but does not gate: nothing in this service calls air-infra
  yet, since the Redis-backed session store that will need it is Phase 2 work.

* ``/v1/capabilities`` — what can this deployment do *for the caller in front of
  it*? Per-principal, because a customer key and a business key see different
  routes and a different guardrail profile from the same process.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from air_orchestrator_service import __version__
from air_orchestrator_service.api.deps import AppState, get_app_state, require_principal
from air_orchestrator_service.api.errors import InsufficientScopeError
from air_orchestrator_service.constants import SCOPE_ADMIN_READ, DownstreamService
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.schemas.common import (
    CapabilitiesResponse,
    DependencyStatus,
    HealthResponse,
    Principal,
    ReadyResponse,
)

__all__ = ["build_metrics_router", "router"]

router = APIRouter(tags=["system"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness",
    description="Process is up. Consults nothing; never fails while the process can respond.",
)
async def health(state: Annotated[AppState, Depends(get_app_state)]) -> HealthResponse:
    return HealthResponse(service=state.settings.obs.service_name, version=__version__)


@router.get(
    "/ready",
    response_model=ReadyResponse,
    summary="Readiness",
    description=(
        "Whether this replica can serve a turn. Gates on air-llm, which is the one "
        "dependency without which no answer can be synthesised. Other services, "
        "including air-infra, are reported for operator visibility but do not "
        "affect readiness."
    ),
    responses={HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyResponse}},
)
async def ready(
    response: Response,
    state: Annotated[AppState, Depends(get_app_state)],
) -> ReadyResponse:
    llm = await state.llm.probe()
    infra = await state.infra.probe()
    dependencies = [llm, infra, *_configured_downstreams(state)]

    ready_now = bool(llm.reachable)
    if not ready_now:
        # A body on the 503 as well as the status: an orchestrator reads the status,
        # but the human paged at 3am needs to know *which* dependency, and a bare
        # 503 sends them to the logs to find out.
        response.status_code = HTTP_503_SERVICE_UNAVAILABLE

    return ReadyResponse(
        ready=ready_now,
        service=state.settings.obs.service_name,
        version=__version__,
        checked_at=datetime.now(UTC),
        dependencies=dependencies,
    )


def _configured_downstreams(state: AppState) -> list[DependencyStatus]:
    """The optional services, reported from configuration without probing them.

    Not probed here on purpose. Readiness is on the hot path for every
    orchestrator health check, and fanning out to five services on each one would
    make this endpoint the most expensive route in the service. Live reachability
    is capability discovery's job (Phase 3); this reports intent, and
    ``reachable=None`` says honestly that nothing was measured.
    """
    downstream = state.settings.downstream
    statuses: list[DependencyStatus] = []
    for service in (
        DownstreamService.CLASSIFIER,
        DownstreamService.RAG,
        DownstreamService.TOOLS,
        DownstreamService.ACTION,
        DownstreamService.RECOMMENDER,
    ):
        config = downstream.for_service(service)
        if config is None:  # pragma: no cover — every optional service is mapped
            continue
        statuses.append(
            DependencyStatus(
                service=service,
                configured=config.configured,
                reachable=None,
                detail=None if config.configured else "not enabled in this deployment",
            )
        )
    return statuses


@router.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    summary="What this deployment can do for you",
    description=(
        "Reported per principal: routes, guardrail profile, stream shapes, session "
        "and turn ceilings as they apply to the calling key's channel."
    ),
)
async def capabilities(
    state: Annotated[AppState, Depends(get_app_state)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> CapabilitiesResponse:
    # Deliberately gated on a scope rather than open to any valid key: the
    # response enumerates which services a deployment talks to, which is
    # reconnaissance if it leaks.
    if not principal.has_scope(SCOPE_ADMIN_READ):
        raise InsufficientScopeError(f"This key lacks the '{SCOPE_ADMIN_READ}' scope.")

    settings = state.settings
    profile = settings.guardrails.for_channel(principal.channel)

    return CapabilitiesResponse(
        service=settings.obs.service_name,
        version=__version__,
        env=settings.app.env,
        channel=principal.channel,
        routes=sorted(settings.downstream.routes()),
        guardrails={
            "injection": profile.injection,
            "redact_pii": profile.redact_pii,
            "grounding": profile.grounding,
            "validate_schema": profile.validate_schema,
        },
        streaming={
            "sse": True,
            "stage_events": True,
            # False until air-llm grows a streaming endpoint. Reported rather
            # than assumed so a client can tell a build that cannot stream tokens
            # from one that simply did not this time (docs/01-hld.md §5, §9).
            "token_deltas": False,
        },
        session={
            "backend": settings.session.backend,
            "ttl_seconds": settings.session.ttl_seconds,
            "proposal_ttl_seconds": settings.session.proposal_ttl_seconds,
            "window_turns": settings.turn.window_turns,
        },
        turn={
            "deadline_ms": settings.turn.deadline_ms,
            "max_cost_usd": settings.turn.max_cost_usd,
            "max_model_calls": settings.turn.max_model_calls,
        },
    )


def build_metrics_router(path: str) -> APIRouter:
    """The Prometheus exposition endpoint, mounted outside ``/v1``.

    Outside the version prefix because a metric name is not part of the API
    contract and must not imply that ``/v2`` would carry different ones.
    """
    metrics_router = APIRouter(tags=["system"])

    @metrics_router.get(
        path,
        summary="Prometheus metrics",
        include_in_schema=False,
        response_class=Response,
    )
    async def prometheus() -> Response:
        return Response(content=metrics.render(), media_type=metrics.CONTENT_TYPE)

    return metrics_router
