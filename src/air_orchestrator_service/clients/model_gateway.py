"""air-model-gateway client — the only model path (Article VII, amended 1.1.0).

Reached over HTTP, never imported as a package: the gateway ships a reference client
(``air_model_gateway_client``), but importing another repository's code would couple
this service's release to theirs, which is exactly what an HTTP contract avoids. The
contract is the gateway's ``docs/openapi.json`` and ``docs/04-integration.md``.

**What this client sends.** ``POST /v1/generate`` with only the fields the gateway's
``GenerateInput`` defines — it rejects anything else with a 422, so a leftover field
fails every call rather than being ignored. ``model`` is a :class:`ModelClass`, never a
model or alias name: which models serve a class for this service is decided in the
gateway's own routing file (``overrides.air-orchestrator-service``).

**How failures are classified.** The gateway already retries a transient provider
failure once and fails over across the class's chain, so a 502 here has been tried
hard. This client therefore never retries; it classifies, so the turn engine can tell
"momentarily unavailable" (connection, timeout, 5xx, 429 ``rate_limited``) from "this
call or this configuration is wrong" (any other 4xx) and from "out of money until the
next UTC day" (429 ``budget_exceeded``). The last two look alike as status codes and
mean opposite things to an operator, which is why the gateway's problem ``code`` is
kept on the error.

**Why readiness asks two questions.** The gateway's ``/v1/ready`` answers "does any
provider respond?" — for *any* caller. It stayed green through the whole period when
every call from this service 404'd. ``/v1/capabilities``, authenticated with this
service's token, answers the question readiness actually means here: is the token
accepted, and is the configured class both granted to this service and backed by a
configured provider in this deployment? It is in-process on the gateway and bills
nothing, unlike a probe ``/v1/generate``. The two run concurrently, each under the
short health timeout.

The ``httpx.AsyncClient`` is created lazily behind a lock: built in ``__init__`` it
would bind to whichever event loop existed at construction, which breaks under a
uvicorn reload and most test harnesses.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, Field

from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import (
    HEADER_API_KEY,
    HEADER_REQUEST_ID,
    DownstreamService,
    ModelClass,
)
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.observability.logging import current_request_id, get_logger
from air_orchestrator_service.schemas.common import DependencyStatus

__all__ = [
    "GenerateResult",
    "GenerateUsage",
    "ModelGatewayClient",
    "ModelGatewayError",
]

logger = get_logger(__name__)

_SERVICE: Final = DownstreamService.MODEL_GATEWAY
_READY_PATH: Final[str] = "/v1/ready"
_CAPABILITIES_PATH: Final[str] = "/v1/capabilities"
_GENERATE_PATH: Final[str] = "/v1/generate"

#: The gateway's problem codes are short snake_case slugs. Anything else is not
#: echoed into a log line or an exception message: the body is upstream-controlled.
_CODE_SHAPE: Final = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_BUDGET_EXCEEDED: Final[str] = "budget_exceeded"


class _Message(BaseModel):
    role: str
    content: str


class _GenerateBody(BaseModel):
    """The subset of the gateway's ``GenerateInput`` this service uses.

    ``extra="forbid"`` here mirrors the gateway's own: a field added by mistake fails
    in this repository's tests instead of as a 422 in production. Never add a field
    the gateway's contract does not define (``task`` was the one that broke).
    """

    model_config = ConfigDict(extra="forbid")

    model: ModelClass
    messages: list[_Message]
    max_tokens: int = Field(ge=1)
    json_schema: dict[str, Any] | None = None
    schema_name: str | None = None
    cache_prefix: bool = True


class GenerateUsage(BaseModel):
    """Wire-shaped token counters — the gateway's own field names, not this repo's
    ``schemas.common.Usage``. The engine maps one to the other, so each can drift
    without the other noticing."""

    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class GenerateResult(BaseModel):
    """The gateway's ``GenerateOutput``, trimmed to what the engine reads.

    Every field defaults: a missing field degrades to an absent value rather than
    failing the call — whether the answer is *usable* is the engine's decision, and
    ``refusal`` and ``finish_reason`` are what it decides on.
    """

    model_config = ConfigDict(extra="ignore")

    content: str = ""
    #: The provider's safety layer declined. ``content`` is then empty, and the
    #: call is still billed.
    refusal: bool = False
    #: Normalised by the gateway across providers: ``stop`` | ``length`` |
    #: ``refusal`` | ``other``.
    finish_reason: str | None = None
    cost_usd: float = 0.0
    cached: bool = False
    usage: GenerateUsage = Field(default_factory=GenerateUsage)

    @property
    def cut_off(self) -> bool:
        """Stopped at ``max_tokens``: prose is merely short, but structured output
        is invalid JSON and must not be parsed."""
        return self.finish_reason == "length"


class ModelGatewayError(Exception):
    """A classified failure from :meth:`ModelGatewayClient.generate`.

    ``retryable`` says whether trying again could plausibly succeed. ``code`` is the
    gateway's problem code when it sent one (``budget_exceeded``, ``rate_limited``,
    ``provider_rejected``, ``policy_denied`` …) and ``None`` for a transport failure.
    The message never carries a URL, a body, or the token.
    """

    def __init__(
        self, message: str, *, retryable: bool, status: int | None = None, code: str | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.code = code


class ModelGatewayClient:
    """Thin async client for air-model-gateway. See the module docstring."""

    def __init__(self, settings: Settings) -> None:
        self._config = settings.model_gateway
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def model_class(self) -> ModelClass:
        return self._config.model_class

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

    # ── Readiness ─────────────────────────────────────────────────────────────

    async def probe(self) -> DependencyStatus:
        """Whether the gateway can serve *this service*, for ``/v1/ready``.

        Never raises: a readiness endpoint that can fail reports nothing at the
        moment it matters most, so every failure becomes ``reachable=False`` plus a
        short reason that names the cause and never a URL, a body, or the token.
        """
        client = await self._http()
        started = time.perf_counter()
        timeout = self._config.health_timeout_s
        try:
            ready, caps = await asyncio.gather(
                client.get(_READY_PATH, timeout=timeout),
                client.get(_CAPABILITIES_PATH, timeout=timeout),
            )
        except httpx.TimeoutException:
            metrics.record_downstream_call(service=_SERVICE, outcome="timeout")
            return self._status(started, reachable=False, detail=f"no response within {timeout}s")
        except httpx.HTTPError as exc:
            metrics.record_downstream_call(service=_SERVICE, outcome="unavailable")
            # The exception type, never ``str(exc)``: httpx renders the full URL
            # into its message, and the base URL can embed credentials.
            logger.warning("model_gateway.probe_failed", error=type(exc).__name__)
            return self._status(
                started, reachable=False, detail=f"connection failed ({type(exc).__name__})"
            )

        detail = self._unservable_reason(ready, caps)
        metrics.record_downstream_call(service=_SERVICE, outcome="error" if detail else "ok")
        return self._status(started, reachable=detail is None, detail=detail)

    def _unservable_reason(self, ready: httpx.Response, caps: httpx.Response) -> str | None:
        """``None`` when this service can be served; otherwise why not.

        Status codes and our own class name only — never the response bodies, which
        are upstream-controlled and this readiness body is unauthenticated.
        """
        if ready.status_code != httpx.codes.OK:
            return f"air-model-gateway returned HTTP {ready.status_code} (no provider answers)"
        if caps.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            return f"air-model-gateway rejected this service's token (HTTP {caps.status_code})"
        if caps.status_code != httpx.codes.OK:
            return f"air-model-gateway capabilities returned HTTP {caps.status_code}"
        name = self._config.model_class.value
        try:
            choice = caps.json()["generate"][name]
            allowed, available = bool(choice["allowed"]), bool(choice["available"])
        except (ValueError, KeyError, TypeError):
            return f"model class '{name}' is not offered to this service"
        if not allowed:
            return f"model class '{name}' is not granted to this service"
        if not available:
            return f"model class '{name}' has no configured provider in this gateway deployment"
        return None

    def _status(self, started: float, *, reachable: bool, detail: str | None) -> DependencyStatus:
        return DependencyStatus(
            service=_SERVICE,
            configured=True,
            reachable=reachable,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            detail=detail,
        )

    # ── Generation ────────────────────────────────────────────────────────────

    def _body(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
        json_schema: dict[str, Any] | None,
        schema_name: str | None,
    ) -> dict[str, Any]:
        return _GenerateBody(
            model=self._config.model_class,
            messages=[_Message(**m) for m in messages],
            max_tokens=max_tokens,
            json_schema=json_schema,
            schema_name=schema_name,
        ).model_dump(mode="json", exclude_none=True)

    @staticmethod
    def _trace_headers() -> dict[str, str]:
        """The caller's ``X-Request-ID``, so one id follows a turn into the
        gateway's audit line instead of starting over there."""
        request_id = current_request_id()
        return {HEADER_REQUEST_ID: request_id} if request_id is not None else {}

    async def generate(
        self,
        *,
        messages: list[dict[str, str]],
        max_tokens: int = 1024,
        json_schema: dict[str, Any] | None = None,
        schema_name: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 — httpx's own per-call override
    ) -> GenerateResult:
        """One non-streaming generation. Raises :class:`ModelGatewayError`.

        Unlike :meth:`probe`, this does not swallow failures: a caller that needs
        the answer must be able to tell "no answer" from "an empty one", and the
        engine owns the degraded fallback.

        ``timeout`` overrides the configured ``timeout_s``: once a turn has spent part
        of its deadline on earlier stages, the remainder is what bounds this call.
        """
        client = await self._http()
        body = self._body(messages, max_tokens, json_schema, schema_name)
        call_timeout = self._config.timeout_s if timeout is None else timeout
        try:
            response = await client.post(
                _GENERATE_PATH, json=body, timeout=call_timeout, headers=self._trace_headers()
            )
        except httpx.TimeoutException as exc:
            metrics.record_downstream_call(service=_SERVICE, outcome="timeout")
            raise ModelGatewayError(f"no response within {call_timeout}s", retryable=True) from exc
        except httpx.HTTPError as exc:
            metrics.record_downstream_call(service=_SERVICE, outcome="unavailable")
            raise ModelGatewayError(
                f"connection failed ({type(exc).__name__})", retryable=True
            ) from exc

        if response.status_code == httpx.codes.OK:
            metrics.record_downstream_call(service=_SERVICE, outcome="ok")
            return GenerateResult.model_validate(response.json())
        raise self._classify(response)

    def _classify(self, response: httpx.Response) -> ModelGatewayError:
        """Turn a non-200 into a classified error, logging it at a level that
        matches whose problem it is."""
        metrics.record_downstream_call(service=_SERVICE, outcome="error")
        status = response.status_code
        code = _problem_code(response)
        throttled = status == httpx.codes.TOO_MANY_REQUESTS and code != _BUDGET_EXCEEDED
        retryable = status >= 500 or throttled
        # A 4xx that is not a throttle is this service's request or configuration
        # being wrong (a contract break, a missing grant, a spent budget). That is an
        # error to fix, not capacity to wait out, so it logs at error level — the
        # previous contract break hid for exactly as long as it logged as a warning.
        log = logger.warning if retryable else logger.error
        log("model_gateway.call_failed", status=status, code=code, retryable=retryable)
        suffix = f" ({code})" if code else ""
        return ModelGatewayError(
            f"air-model-gateway returned HTTP {status}{suffix}",
            retryable=retryable,
            status=status,
            code=code,
        )

    async def aclose(self) -> None:
        """Release the connection pool. Idempotent, so a double shutdown is safe."""
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _problem_code(response: httpx.Response) -> str | None:
    """The gateway's RFC 9457 ``code``, if it is a well-formed slug; else ``None``."""
    try:
        return _slug(response.json().get("code"))
    except (ValueError, AttributeError):
        return None


def _slug(code: object) -> str | None:
    return code if isinstance(code, str) and _CODE_SHAPE.match(code) else None
