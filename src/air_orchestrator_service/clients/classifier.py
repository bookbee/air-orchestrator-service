"""air-classifier client — sentiment, intent and urgency for the ``CLASSIFY`` stage.

Spec 0005. Reached over HTTP like every AIR service; the contract is air-classifier's
``POST /v1/classify`` (``schemas/classify.py``), whose request is ``extra="forbid"``. So
this client sends only ``text``, ``metadata`` and ``options`` (``latency_budget_ms``,
``max_cost_usd``, and ``max_tier`` when configured), and a field added here without the
classifier adding it too fails every call loudly.

**Why it passes on the turn's limits.** Classification is enrichment, and the answer is
the product. So the call gets at most ``deadline_share`` of the turn's deadline, and at
most what the turn's cost ceiling leaves once synthesis is reserved (spec 0005,
``REQ-CLS-014``/``015``; spec 0008). Without both, a slow cloud-tier verdict starved
synthesis of time, and its spend was invisible to every ceiling.

**Why the response model is narrow and loose.** :class:`Classification` declares only
the fields the engine reads, ignores the rest, and keeps labels as plain strings rather
than mirroring air-classifier's enums. A label added to air-classifier's vocabulary is
then a new value here, not a validation failure on every turn. Mirroring the enums
would make the two repos' release order matter. The cost is that a label is *untrusted
downstream text*, so it is rendered into this service's output (stage detail, logs) only
through :func:`label_slug`.

**Why the latency budget is the timeout less a headroom.** air-classifier's ladder
climbs from rules to a cloud LLM, and ``options.latency_budget_ms`` is its own governor on
how far. When the budget runs out, the ladder stops and returns the best verdict it has,
but that response lands *after* the budget, 10 to 25 ms later on a laptop and more across a
network. So the budget is this client's timeout less :data:`RESPONSE_HEADROOM_MS`. The
ladder then stops, and its verdict arrives, before this client would abandon the call,
rather than the call being cut off mid-climb with nothing to show for the tiers already
paid for. A budget equal to the timeout loses that race every time the ladder uses its
whole budget (spec 0005, ``REQ-CLS-004``).

**What it shares.** Headers, the failure taxonomy, the metric, the circuit breaker and
the rule that no URL or body reaches an error all come from ``clients/base.py``
(spec 0010). This module adds only the classifier's request and response shapes.
"""

from __future__ import annotations

import re
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from air_orchestrator_service.clients.base import DownstreamClient, DownstreamError
from air_orchestrator_service.config import ClassifierSettings
from air_orchestrator_service.constants import DownstreamService

__all__ = [
    "MIN_CALL_MS",
    "RESPONSE_HEADROOM_MS",
    "Classification",
    "ClassifierClient",
    "ClassifierError",
    "ClassifierUsage",
    "label_slug",
]

_CLASSIFY_PATH: Final[str] = "/v1/classify"
#: air-classifier's ``Options.latency_budget_ms`` bounds (``ge=50, le=30_000``).
_MIN_BUDGET_MS: Final[int] = 50
_MAX_BUDGET_MS: Final[int] = 30_000
#: air-classifier's ``Options.max_cost_usd`` bounds (``ge=0, le=10``).
_MAX_COST_USD: Final[float] = 10.0
#: Time left between the classifier's budget expiring and this client's timeout, for
#: its fallback verdict to arrive. About ten times the overrun measured locally.
RESPONSE_HEADROOM_MS: Final[int] = 250
#: The shortest bound worth a call: the classifier's minimum budget plus the headroom.
MIN_CALL_MS: Final[int] = _MIN_BUDGET_MS + RESPONSE_HEADROOM_MS
_LABEL_SHAPE: Final = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class ClassifierError(DownstreamError):
    """The classifier did not produce a usable verdict. ``reason`` is one of spec
    0010's four words, never a body."""


class _Labeled(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str
    confidence: float = 0.0


class ClassifierUsage(BaseModel):
    """What air-classifier says the call cost (its ``RequestUsage``), counted by
    spec 0008. Its own estimate, from the gateway's prices: this service has no
    better number."""

    model_config = ConfigDict(extra="ignore")

    est_cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


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
    #: Absent counts as free (``REQ-CLS-017``), which only ever under-counts a
    #: classifier too old to report it.
    usage: ClassifierUsage = ClassifierUsage()

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


class ClassifierClient(DownstreamClient):
    """Thin async client for air-classifier. See the module docstring."""

    def __init__(self, config: ClassifierSettings) -> None:
        super().__init__(DownstreamService.CLASSIFIER, config)
        self._classifier_config = config

    async def classify(
        self, text: str, *, tenant: str, timeout_s: float, max_cost_usd: float | None = None
    ) -> Classification:
        """One verdict on ``text`` (already input-masked by the caller), or raise
        :class:`ClassifierError`. ``max_cost_usd`` is what the turn can still
        afford (``REQ-CLS-015``)."""
        budget_ms = max(
            _MIN_BUDGET_MS, min(_MAX_BUDGET_MS, int(timeout_s * 1000) - RESPONSE_HEADROOM_MS)
        )
        options: dict[str, Any] = {"latency_budget_ms": budget_ms}
        if max_cost_usd is not None:
            options["max_cost_usd"] = round(min(max(max_cost_usd, 0.0), _MAX_COST_USD), 6)
        if self._classifier_config.max_tier is not None:
            options["max_tier"] = self._classifier_config.max_tier
        body: dict[str, Any] = {
            "text": text,
            # Echoed back and never interpreted by air-classifier: the tenant
            # rides here so its audit trail correlates with ours.
            "metadata": {"tenant": tenant},
            "options": options,
        }
        return await self.post_json(
            _CLASSIFY_PATH, body, timeout_s=timeout_s, parse=Classification.model_validate
        )

    def _error(self, message: str, *, reason: str, status: int | None) -> DownstreamError:
        return ClassifierError(message, service=self.service, reason=reason, status=status)
