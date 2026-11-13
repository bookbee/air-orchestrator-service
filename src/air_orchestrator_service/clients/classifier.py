"""air-classifier client — sentiment, intent and urgency for the ``CLASSIFY`` stage.

Spec 0005. Reached over HTTP like every AIR service; the contract is air-classifier's
``POST /v1/classify`` (``schemas/classify.py``), whose request is ``extra="forbid"``. So
this client sends only ``text``, ``metadata`` and ``options.latency_budget_ms``, and a
field added here without the classifier adding it too fails every call loudly.

**Why the response model is narrow and loose.** :class:`Classification` declares only
the fields the engine reads, ignores the rest, and keeps labels as plain strings rather
than mirroring air-classifier's enums. A label added to air-classifier's vocabulary is
then a new value here, not a validation failure on every turn. Mirroring the enums
would make the two repos' release order matter. The cost is that a label is *untrusted
downstream text*, so it is rendered into this service's output (stage detail, logs) only
through :func:`label_slug`.

**Why the latency budget equals the timeout.** air-classifier's ladder climbs from rules
to a cloud LLM, and ``options.latency_budget_ms`` is its own governor on how far. Passing
the same bound this client will wait means the ladder stops itself and returns a cheaper
verdict before this client would abandon the call, instead of being cut off mid-climb
with nothing to show for the tiers already paid for.

**Why one attempt.** The stage degrades softly (spec 0005 §7), and retries with a
circuit breaker belong in the shared ``clients/base.py`` that Phase 3 lifts from
air-classifier's ``resilience/`` (``REQ-CLS-013``). Every failure, whether connection,
timeout, non-200 or unreadable body, becomes one :class:`ClassifierError` that says which.

The ``httpx.AsyncClient`` is built lazily behind a lock, for the event-loop reason
``clients/model_gateway.py`` gives.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Final

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from air_orchestrator_service.config import ServiceSettings
from air_orchestrator_service.constants import (
    HEADER_API_KEY,
    HEADER_REQUEST_ID,
    DownstreamService,
)
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.observability.logging import current_request_id, get_logger

__all__ = ["Classification", "ClassifierClient", "ClassifierError", "label_slug"]

logger = get_logger(__name__)

_SERVICE: Final = DownstreamService.CLASSIFIER
_CLASSIFY_PATH: Final[str] = "/v1/classify"
#: air-classifier's ``Options.latency_budget_ms`` bounds (``ge=50, le=30_000``).
MIN_BUDGET_MS: Final[int] = 50
_MAX_BUDGET_MS: Final[int] = 30_000
_LABEL_SHAPE: Final = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class ClassifierError(Exception):
    """The classifier did not produce a usable verdict. ``reason`` is one of
    ``timeout`` | ``unavailable`` | ``error`` (the metric outcome), never a body."""

    def __init__(self, message: str, *, reason: str, status: int | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.status = status


class _Labeled(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str
    confidence: float = 0.0


class Classification(BaseModel):
    """The slice of air-classifier's ``ClassificationResponse`` this service reads."""

    model_config = ConfigDict(extra="ignore")

    sentiment: _Labeled
    urgency: _Labeled
    intent: _Labeled | None = None
    requires_human: bool = False
    decided_by: str = ""
    #: air-classifier's own degradation (a tier failed, a lower one answered) —
    #: still a verdict, so the stage is ``ok`` and says so in its detail.
    degraded: bool = False

    def summary(self) -> str:
        """``sentiment=… intent=… urgency=…`` for a stage detail: slugs only."""
        intent = label_slug(self.intent.label) if self.intent else "none"
        parts = [
            f"sentiment={label_slug(self.sentiment.label)}",
            f"intent={intent}",
            f"urgency={label_slug(self.urgency.label)}",
        ]
        if self.requires_human:
            parts.append("requires_human")
        if self.degraded:
            parts.append("classifier_degraded")
        return " ".join(parts)


def label_slug(value: str) -> str:
    """A downstream label made safe to render: itself if a short lowercase slug,
    else ``other``. Labels are a closed vocabulary upstream, so ``other`` means a
    contract surprise, never a normal verdict."""
    return value if _LABEL_SHAPE.match(value) else "other"


class ClassifierClient:
    """Thin async client for air-classifier. See the module docstring."""

    def __init__(self, config: ServiceSettings) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        # Explicit, so a future attribute holding the raw key cannot leak through
        # a default repr into a traceback (Article IX).
        return f"ClassifierClient(base_url={self._config.base_url!r})"

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

    def bound_s(self, remaining_s: float | None) -> float:
        """How long a call may take: the configured timeout, narrowed (never
        widened) by what is left of the turn."""
        configured = self._config.timeout_ms / 1000
        return configured if remaining_s is None else min(configured, remaining_s)

    async def classify(self, text: str, *, tenant: str, timeout_s: float) -> Classification:
        """One verdict on ``text`` (already input-masked by the caller), or raise
        :class:`ClassifierError`."""
        budget_ms = max(MIN_BUDGET_MS, min(_MAX_BUDGET_MS, int(timeout_s * 1000)))
        body: dict[str, Any] = {
            "text": text,
            # Echoed back and never interpreted by air-classifier: the tenant
            # rides here so its audit trail correlates with ours.
            "metadata": {"tenant": tenant},
            "options": {"latency_budget_ms": budget_ms},
        }
        headers = {}
        if (request_id := current_request_id()) is not None:
            headers[HEADER_REQUEST_ID] = request_id

        client = await self._http()
        try:
            response = await client.post(
                _CLASSIFY_PATH, json=body, headers=headers, timeout=timeout_s
            )
        except httpx.TimeoutException as exc:
            raise self._failed("timeout", "air-classifier timed out") from exc
        except httpx.HTTPError as exc:
            raise self._failed("unavailable", "air-classifier is unreachable") from exc

        if response.status_code != httpx.codes.OK:
            raise self._failed(
                "error",
                f"air-classifier returned HTTP {response.status_code}",
                status=response.status_code,
            )
        try:
            verdict = Classification.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            # The body is not echoed: it is a downstream's text, and may hold
            # the very content the classifier was asked about.
            raise self._failed("error", "air-classifier returned an unreadable verdict") from exc

        metrics.record_downstream_call(service=_SERVICE, outcome="ok")
        return verdict

    def _failed(self, reason: str, message: str, *, status: int | None = None) -> ClassifierError:
        metrics.record_downstream_call(service=_SERVICE, outcome=reason)
        logger.warning("classifier.call_failed", reason=reason, status=status)
        return ClassifierError(message, reason=reason, status=status)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
