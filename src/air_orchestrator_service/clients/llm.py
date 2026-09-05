"""air-llm client — the model gateway. The only model path (G7).

Reached over HTTP, not imported as a package — the same shape as
`clients/infra.py`, following air-classifier's precedent for talking to air-llm
(`providers/air_llm_provider.py`).

This is the one downstream whose absence is fatal to a turn — no synthesis is
possible without it — so it has no ``enabled`` flag and its probe is what
``/v1/ready`` gates on.

The probe hits ``/v1/ready``, not ``/v1/health``: air-llm's own health route is
dependency-free liveness and never reflects whether a provider actually answers,
while its ready route is explicitly gated on "at least one provider reachable"
(air-llm's `api/v1/system.py`) — the one signal that actually answers "can a turn
be synthesised right now." air-classifier's own air-llm adapter probes the same
path for the same reason.

Phase 0 implemented the probe only; ``chat`` arrives with the turn engine in
Phase 2, wire-shaped exactly like the ``chat()``/``embed()`` methods already
built for air-tools/air-rag this session (``POST /v1/inference``,
``task="chat"``, a role/routing alias as ``model``, an optional
``json_schema`` for schema-constrained generation).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field

from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import HEADER_API_KEY, HEADER_REQUEST_ID, DownstreamService
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.observability.logging import current_request_id, get_logger
from air_orchestrator_service.schemas.common import DependencyStatus

__all__ = ["ChatResult", "ChatUsage", "LlmCallError", "LlmClient"]

logger = get_logger(__name__)

_READY_PATH: Final[str] = "/v1/ready"
_INFERENCE_PATH: Final[str] = "/v1/inference"


class _ChatMessage(BaseModel):
    role: str
    content: str


class _ChatBody(BaseModel):
    task: str = "chat"
    model: str
    messages: list[_ChatMessage]
    max_tokens: int
    json_schema: dict[str, Any] | None = None
    schema_name: str | None = None
    cache_prefix: bool = True


class ChatUsage(BaseModel):
    """Wire-shaped token counters — air-llm's own field names, not this
    repo's `schemas.common.Usage`. The turn engine maps one to the other;
    keeping them separate here is what lets each drift independently if
    air-llm's accounting ever adds a field this repo doesn't care about."""

    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class ChatResult(BaseModel):
    """air-llm's `/v1/inference` response, trimmed to what the turn engine
    needs. Every field is defaulted: a missing or malformed field degrades to
    an absent value rather than failing the call — whether the *answer* is
    usable is `TurnEngine`'s call, not this envelope's."""

    model_config = ConfigDict(extra="ignore")

    refusal: bool = False
    finish_reason: str | None = None
    content: str = ""
    cost_usd: float = 0.0
    usage: ChatUsage = Field(default_factory=ChatUsage)


class LlmCallError(Exception):
    """A classified failure from `chat()`.

    `retryable` says whether trying again could plausibly succeed —
    connection failures, timeouts, and 5xx/429 responses are; any other 4xx
    is not, since the same request would just fail the same way again. This
    repo does not retry on its own (that is real machinery — backoff,
    idempotency — not built here); the flag exists so `TurnEngine` can
    distinguish "the model is momentarily unavailable" from "this call was
    wrong" when it degrades.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class LlmClient:
    """Thin async client for the air-llm gateway.

    The ``httpx.AsyncClient`` is created lazily and behind a lock. Creating it in
    ``__init__`` would bind it to whichever event loop happened to be running at
    construction time, which breaks the moment the app is built in one loop and
    served in another — the exact shape of a uvicorn reload and of most test
    harnesses.
    """

    def __init__(self, settings: Settings) -> None:
        self._config = settings.llm
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
        """Whether air-llm itself reports ready, for ``/v1/ready``.

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
            response = await client.get(_READY_PATH, timeout=self._config.health_timeout_s)
            elapsed_ms = (time.perf_counter() - started) * 1000
            ok = response.status_code == httpx.codes.OK
            metrics.record_downstream_call(
                service=DownstreamService.LLM, outcome="ok" if ok else "error"
            )
            return DependencyStatus(
                service=DownstreamService.LLM,
                configured=True,
                reachable=ok,
                latency_ms=round(elapsed_ms, 3),
                # The status code, not the body: an upstream error body can carry a
                # stack trace or an internal hostname, and this response is
                # unauthenticated.
                detail=None if ok else f"air-llm returned HTTP {response.status_code}",
            )
        except httpx.TimeoutException:
            metrics.record_downstream_call(service=DownstreamService.LLM, outcome="timeout")
            return DependencyStatus(
                service=DownstreamService.LLM,
                configured=True,
                reachable=False,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                detail=f"no response within {self._config.health_timeout_s}s",
            )
        except httpx.HTTPError as exc:
            metrics.record_downstream_call(service=DownstreamService.LLM, outcome="unavailable")
            # The exception type, never ``str(exc)``: httpx renders the full URL into
            # its message, and the base URL can embed credentials.
            logger.warning("llm.probe_failed", error=type(exc).__name__)
            return DependencyStatus(
                service=DownstreamService.LLM,
                configured=True,
                reachable=False,
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
                detail=f"connection failed ({type(exc).__name__})",
            )

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int = 1024,
        json_schema: dict[str, Any] | None = None,
        schema_name: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — httpx's own per-call override
    ) -> ChatResult:
        """One synthesis call. Raises `LlmCallError` on failure — unlike
        `probe()`, this does not swallow the error, because a caller that
        actually needs the answer must be able to tell "no answer" from "an
        empty one"; the turn engine decides its own degraded fallback.

        `timeout` overrides the configured `timeout_s` when given — the turn
        engine's own remaining deadline, not this client's fixed default, is
        what should bound a call once a turn has already spent part of its
        budget on earlier stages.

        The caller's `X-Request-ID` is forwarded automatically (via
        `current_request_id()`) so a trace survives this hop rather than
        starting over at air-llm.
        """
        client = await self._http()
        body = _ChatBody(
            model=model,
            messages=[_ChatMessage(**m) for m in messages],
            max_tokens=max_tokens,
            json_schema=json_schema,
            schema_name=schema_name,
        ).model_dump(exclude_none=True)
        headers: dict[str, str] = {}
        request_id = current_request_id()
        if request_id is not None:
            headers[HEADER_REQUEST_ID] = request_id
        call_timeout = self._config.timeout_s if timeout is None else timeout

        try:
            response = await client.post(
                _INFERENCE_PATH, json=body, timeout=call_timeout, headers=headers
            )
        except httpx.TimeoutException as exc:
            metrics.record_downstream_call(service=DownstreamService.LLM, outcome="timeout")
            raise LlmCallError(f"no response within {call_timeout}s", retryable=True) from exc
        except httpx.HTTPError as exc:
            metrics.record_downstream_call(service=DownstreamService.LLM, outcome="unavailable")
            raise LlmCallError(f"connection failed ({type(exc).__name__})", retryable=True) from exc

        if response.status_code == httpx.codes.OK:
            metrics.record_downstream_call(service=DownstreamService.LLM, outcome="ok")
            return ChatResult.model_validate(response.json())

        metrics.record_downstream_call(service=DownstreamService.LLM, outcome="error")
        # 5xx and 429 are the model gateway or a provider having a bad moment;
        # any other 4xx is this call itself being wrong, and retrying an
        # unchanged request would just fail the same way again.
        status = response.status_code
        retryable = status >= 500 or status == httpx.codes.TOO_MANY_REQUESTS
        raise LlmCallError(f"air-llm returned HTTP {response.status_code}", retryable=retryable)

    async def aclose(self) -> None:
        """Release the connection pool. Idempotent, so a double shutdown is safe."""
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            await self._client.aclose()
            self._client = None
