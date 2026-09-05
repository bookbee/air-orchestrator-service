"""air-infra client — the broker for Redis/Postgres/Mongo and secrets.

Not the model path: that's `air-llm` (see `clients/llm.py`), a separate service
air-infra's own model gateway was extracted into. air-infra is a **service**
dependency, reached over HTTP, not a package one — following air-classifier's
precedent for air-llm (`providers/air_llm_provider.py` talks over httpx rather
than importing a client package) and keeping the two repos free of the
build-time coupling a path dependency would create. The contract is air-infra's
published API; httpx is the transport.

Nothing in this service calls air-infra yet — Phase 0 implements the probe only,
reported on ``/v1/ready`` for operator visibility but not gating it (that's
air-llm's job). The store brokering arrives with the session engine in Phase 2,
at which point air-infra becomes a real dependency again and its readiness
weight should be revisited.
"""

from __future__ import annotations

import asyncio
import time
from typing import Final

import httpx

from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import HEADER_API_KEY, DownstreamService
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.observability.logging import get_logger
from air_orchestrator_service.schemas.common import DependencyStatus

__all__ = ["InfraClient"]

logger = get_logger(__name__)

_HEALTH_PATH: Final[str] = "/v1/health"


class InfraClient:
    """Thin async client for the air-infra gateway.

    The ``httpx.AsyncClient`` is created lazily and behind a lock. Creating it in
    ``__init__`` would bind it to whichever event loop happened to be running at
    construction time, which breaks the moment the app is built in one loop and
    served in another — the exact shape of a uvicorn reload and of most test
    harnesses.
    """

    def __init__(self, settings: Settings) -> None:
        self._config = settings.infra
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._lock:
                # Re-checked inside the lock: two concurrent first calls would
                # otherwise each build a client and leak one connection pool.
                if self._client is None:
                    headers = {}
                    if self._config.api_key is not None:
                        headers[HEADER_API_KEY] = self._config.api_key.get_secret_value()
                    self._client = httpx.AsyncClient(
                        base_url=self._config.base_url,
                        timeout=self._config.timeout_s,
                        headers=headers,
                    )
        return self._client

    async def probe(self) -> DependencyStatus:
        """Liveness of the gateway, for ``/v1/ready``.

        Uses ``health_timeout_s`` rather than the turn timeout: a probe that waits as
        long as a real request would turns a health check into an outage detector
        that fires far too late to be useful.

        Never raises. A readiness endpoint that can fail is a readiness endpoint that
        reports nothing at the moment it matters most, so every failure mode becomes
        ``reachable=False`` plus a short, non-leaking reason.
        """
        client = await self._http()
        started = time.perf_counter()
        try:
            response = await client.get(_HEALTH_PATH, timeout=self._config.health_timeout_s)
            elapsed_ms = (time.perf_counter() - started) * 1000
            ok = response.status_code == httpx.codes.OK
            metrics.record_downstream_call(
                service=DownstreamService.INFRA, outcome="ok" if ok else "error"
            )
            return DependencyStatus(
                service=DownstreamService.INFRA,
                configured=True,
                reachable=ok,
                latency_ms=round(elapsed_ms, 3),
                # The status code, not the body: an upstream error body can carry a
                # stack trace or an internal hostname, and this response is
                # unauthenticated.
                detail=None if ok else f"gateway returned HTTP {response.status_code}",
            )
        except httpx.TimeoutException:
            metrics.record_downstream_call(service=DownstreamService.INFRA, outcome="timeout")
            return DependencyStatus(
                service=DownstreamService.INFRA,
                configured=True,
                reachable=False,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                detail=f"no response within {self._config.health_timeout_s}s",
            )
        except httpx.HTTPError as exc:
            metrics.record_downstream_call(service=DownstreamService.INFRA, outcome="unavailable")
            # The exception type, never ``str(exc)``: httpx renders the full URL into
            # its message, and the base URL can embed credentials.
            logger.warning("infra.probe_failed", error=type(exc).__name__)
            return DependencyStatus(
                service=DownstreamService.INFRA,
                configured=True,
                reachable=False,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                detail=f"connection failed ({type(exc).__name__})",
            )

    async def aclose(self) -> None:
        """Release the connection pool. Idempotent, so a double shutdown is safe."""
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            await self._client.aclose()
            self._client = None
