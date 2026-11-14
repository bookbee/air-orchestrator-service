"""What every optional downstream call has in common — spec 0010.

air-classifier, air-rag-service, and air-tools after them, are each reached the same way: one POST
of JSON, the key in ``X-API-Key``, the turn's request id in ``X-Request-ID``, and a bound
taken from the turn's deadline. What can go wrong is also the same: no connection, no
answer in time, a status that is not 2xx, a body that does not parse. This module is the
one place that turns those into a :class:`DownstreamError` carrying one of four reason
words. Each reason word is also the metric's outcome label. It is the one place, too,
that guarantees no URL, key or upstream body reaches the error's message (Article IX).
A client per service with its own copy of this code is how one of them eventually
renders a URL into a stage detail.

**Why one attempt and no retry.** A downstream call in a turn gets a share of one deadline.
A retry spends that share again on a service that has just failed, and the answer is the
product. The service is protected from being hammered by the breaker, not by
politeness.

**Why a circuit breaker.** Without one, a dead service costs every turn its whole timeout
share until someone notices. After ``breaker_threshold`` consecutive failures the breaker
opens. Calls then fail at once with ``breaker_open`` and make no request. After
``breaker_cooldown_s``, one call is let through to test the service: success closes the
breaker, failure reopens it. Lifted from air-classifier's ``resilience/breaker.py``, less
its bulkhead and its multi-probe half-open, which a per-turn call with one attempt does
not need. Timing is on a monotonic clock, so a wall-clock step never opens or closes it.

**Why cancellation is not a failure.** A customer closing the tab cancels the turn's task,
and with it the downstream call. That says nothing about the downstream's health.
``asyncio.CancelledError`` is a ``BaseException``, and only ``Exception`` counts.

**Why the breaker lives on the client.** The client lives on ``AppState`` (Article VIII),
so a test that builds a fresh app gets fresh breakers, and one replica's breaker reflects
what that replica has seen.

The ``httpx.AsyncClient`` is built lazily behind a lock, for the event-loop reason
``clients/model_gateway.py`` gives.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any, Final

import httpx
from pydantic import SecretStr

from air_orchestrator_service.config import ServiceSettings
from air_orchestrator_service.constants import (
    HEADER_API_KEY,
    HEADER_REQUEST_ID,
    DownstreamService,
)
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.observability.logging import current_request_id, get_logger
from air_orchestrator_service.schemas.common import DependencyStatus

__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "DownstreamClient",
    "DownstreamError",
]

logger = get_logger(__name__)

_HEALTH_PATH: Final[str] = "/v1/health"


class DownstreamError(Exception):
    """An optional downstream produced nothing usable.

    ``reason`` is one of ``timeout`` | ``unavailable`` | ``error`` | ``breaker_open``,
    the same word as the metric's outcome label. The message is built here from the
    service's name and, for ``error``, the status code. It never contains a URL, a
    key or a body (``REQ-CLB-003``).
    """

    def __init__(
        self,
        message: str,
        *,
        service: DownstreamService,
        reason: str,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.service = service
        self.reason = reason
        self.status = status


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-service failure gate (``REQ-CLB-004``/``005``). See the module docstring.

    Not locked. Every transition runs between awaits, on one event loop, so no two
    coroutines can interleave inside one.
    """

    def __init__(
        self,
        service: DownstreamService,
        *,
        threshold: int,
        cooldown_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probing = False

    @property
    def state(self) -> BreakerState:
        """``half_open`` once the cooldown has passed, before the probe is admitted:
        the breaker is willing to be tested."""
        if self._state is BreakerState.OPEN and self._cooldown_left() <= 0:
            return BreakerState.HALF_OPEN
        return self._state

    @property
    def is_open(self) -> bool:
        """Whether a call would be refused right now."""
        state = self.state
        return state is BreakerState.OPEN or (state is BreakerState.HALF_OPEN and self._probing)

    def admit(self) -> bool:
        """Admit one call, or return ``False`` to refuse it. Admitting the first call
        after the cooldown makes it the probe."""
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.HALF_OPEN and not self._probing:
            self._transition(BreakerState.HALF_OPEN)
            self._probing = True
            return True
        return False

    def succeeded(self) -> None:
        self._probing = False
        self._failures = 0
        self._transition(BreakerState.CLOSED)

    def failed(self) -> None:
        self._probing = False
        self._failures += 1
        if self._state is BreakerState.HALF_OPEN or self._failures >= self._threshold:
            self._opened_at = self._clock()
            self._transition(BreakerState.OPEN)

    def released(self) -> None:
        """The admitted call was abandoned by its caller: no verdict either way
        (``REQ-CLB-006``)."""
        self._probing = False

    def _cooldown_left(self) -> float:
        return self._cooldown_s - (self._clock() - self._opened_at)

    def _transition(self, new: BreakerState) -> None:
        if new is self._state:
            return
        previous, self._state = self._state, new
        logger.info(
            "downstream.breaker",
            service=self._service.value,
            from_state=previous.value,
            to_state=new.value,
            consecutive_failures=self._failures,
        )


class DownstreamClient:
    """One optional AIR service, reached over HTTP. Subclasses add the typed call."""

    def __init__(
        self,
        service: DownstreamService,
        config: ServiceSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self.breaker = CircuitBreaker(
            service,
            threshold=config.breaker_threshold,
            cooldown_s=config.breaker_cooldown_s,
            clock=clock,
        )

    def __repr__(self) -> str:
        # Explicit, so a future attribute holding the raw key cannot leak through a
        # default repr into a traceback (Article IX).
        return f"{type(self).__name__}(base_url={self._config.base_url!r})"

    @property
    def service(self) -> DownstreamService:
        return self._service

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    headers = {}
                    if self._config.api_key is not None:
                        headers[HEADER_API_KEY] = self._config.api_key.get_secret_value()
                    self._client = httpx.AsyncClient(
                        base_url=self._config.base_url, headers=headers
                    )
        return self._client

    def bound_s(self, remaining_s: float | None, *, deadline_s: float | None = None) -> float:
        """How long a call may take: the configured timeout, narrowed (never widened)
        by ``deadline_share`` of the turn's whole deadline and by what is left of it."""
        bound = self._config.timeout_ms / 1000
        if deadline_s is not None:
            bound = min(bound, deadline_s * self._config.deadline_share)
        return bound if remaining_s is None else min(bound, remaining_s)

    async def post_json[T](
        self,
        path: str,
        body: dict[str, Any],
        *,
        timeout_s: float,
        parse: Callable[[Any], T],
        api_key: SecretStr | None = None,
    ) -> T:
        """POST ``body`` and return ``parse(response JSON)``, or raise
        :class:`DownstreamError`. ``parse`` runs inside the breaker's verdict: a body
        that does not validate is a failure like a 500 (``REQ-CLB-002``). ``api_key``
        replaces the configured key for this call only: a service that scopes by key
        (air-rag-service) is called with the caller's tenant's key."""
        if not self.breaker.admit():
            raise self._failed("breaker_open", f"{self._service.value} circuit is open")
        headers = {}
        if api_key is not None:
            headers[HEADER_API_KEY] = api_key.get_secret_value()
        if (request_id := current_request_id()) is not None:
            headers[HEADER_REQUEST_ID] = request_id
        try:
            client = await self._http()
            try:
                response = await client.post(path, json=body, headers=headers, timeout=timeout_s)
            except httpx.TimeoutException as exc:
                raise self._failed("timeout", f"{self._service.value} timed out") from exc
            except httpx.HTTPError as exc:
                raise self._failed("unavailable", f"{self._service.value} is unreachable") from exc
            if not response.is_success:
                raise self._failed(
                    "error",
                    f"{self._service.value} returned HTTP {response.status_code}",
                    status=response.status_code,
                )
            try:
                result = parse(response.json())
            except ValueError as exc:  # pydantic's ValidationError is a ValueError
                # The body is not echoed: it is a downstream's text, and may hold the
                # very content the call was about.
                raise self._failed(
                    "error", f"{self._service.value} returned an unreadable response"
                ) from exc
        except DownstreamError:
            raise
        except BaseException:
            # Cancelled: the caller left, which says nothing about the service.
            self.breaker.released()
            raise
        self.breaker.succeeded()
        metrics.record_downstream_call(service=self._service, outcome="ok")
        return result

    async def probe(self) -> DependencyStatus:
        """Liveness for ``/v1/capabilities`` (``REQ-CLB-007``). Never raises. An open
        breaker is reported without a request. A probe does not move the breaker:
        only real calls are evidence about the service under load."""
        if self.breaker.is_open:
            return DependencyStatus(
                service=self._service, configured=True, reachable=False, detail="circuit open"
            )
        started = time.perf_counter()
        timeout_s = self._config.health_timeout_ms / 1000
        detail: str | None
        try:
            client = await self._http()
            response = await client.get(_HEALTH_PATH, timeout=timeout_s)
        except httpx.TimeoutException:
            reachable, detail = False, f"no response within {timeout_s}s"
        except httpx.HTTPError as exc:
            reachable, detail = False, f"connection failed ({type(exc).__name__})"
        else:
            reachable = response.is_success
            detail = None if reachable else f"returned HTTP {response.status_code}"
        return DependencyStatus(
            service=self._service,
            configured=True,
            reachable=reachable,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            detail=detail,
        )

    def _failed(self, reason: str, message: str, *, status: int | None = None) -> DownstreamError:
        if reason != "breaker_open":
            self.breaker.failed()
        metrics.record_downstream_call(service=self._service, outcome=reason)
        logger.warning(
            "downstream.call_failed", service=self._service.value, reason=reason, status=status
        )
        return self._error(message, reason=reason, status=status)

    def _error(self, message: str, *, reason: str, status: int | None) -> DownstreamError:
        """The exception to raise. A subclass narrows its type, so the engine can
        catch one service's failures without catching another's."""
        return DownstreamError(message, service=self._service, reason=reason, status=status)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
