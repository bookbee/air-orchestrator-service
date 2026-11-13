"""``ClassifierClient`` against a respx air-classifier — spec 0005 (``REQ-CLS-*``)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
import structlog
from pydantic import SecretStr

from air_orchestrator_service.clients.classifier import (
    ClassifierClient,
    ClassifierError,
    label_slug,
)
from air_orchestrator_service.config import ServiceSettings
from tests.conftest import CLASSIFIER_BASE_URL

_KEY = "aircls_secret_value_never_logged"
_URL = f"{CLASSIFIER_BASE_URL}/v1/classify"


def verdict(**overrides: Any) -> dict[str, Any]:
    """A trimmed but real-shaped ``ClassificationResponse``."""
    body: dict[str, Any] = {
        "request_id": "req_cls",
        "sentiment": {"label": "negative", "polarity": -0.7, "confidence": 0.91},
        "urgency": {"label": "high", "confidence": 0.8},
        "intent": {"label": "order_tracking", "confidence": 0.77},
        "topics": [],
        "routing": {"queue": "logistics", "priority": "p2"},
        "requires_human": False,
        "actionability": "actionable",
        "language": {"code": "en", "confidence": 0.99},
        "model": {"name": "rules", "version": "1.0.0"},
        "decided_by": "t1_classifier",
        "degraded": False,
    }
    body.update(overrides)
    return body


@pytest.fixture
async def classifier() -> AsyncIterator[ClassifierClient]:
    client = ClassifierClient(
        ServiceSettings(
            enabled=True, base_url=CLASSIFIER_BASE_URL, api_key=SecretStr(_KEY), timeout_ms=4000
        )
    )
    yield client
    await client.aclose()


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


async def test_the_request_carries_key_request_id_and_tenant(
    classifier: ClassifierClient, mock: respx.MockRouter
) -> None:
    """REQ-CLS-003."""
    route = mock.post(_URL).mock(return_value=httpx.Response(200, json=verdict()))
    structlog.contextvars.bind_contextvars(request_id="req_abc123")
    try:
        await classifier.classify("where is my parcel", tenant="tenant-a", timeout_s=2.0)
    finally:
        structlog.contextvars.unbind_contextvars("request_id")

    sent = route.calls.last.request
    assert sent.headers["X-API-Key"] == _KEY
    assert sent.headers["X-Request-ID"] == "req_abc123"
    body = json.loads(sent.content)
    assert set(body) == {"text", "metadata", "options"}
    assert set(body["options"]) == {"latency_budget_ms"}
    assert body["metadata"] == {"tenant": "tenant-a"}
    assert body["text"] == "where is my parcel"


@pytest.mark.parametrize(("timeout_s", "budget_ms"), [(2.0, 2000), (0.01, 50), (90.0, 30_000)])
async def test_the_latency_budget_matches_the_bound(
    classifier: ClassifierClient, mock: respx.MockRouter, timeout_s: float, budget_ms: int
) -> None:
    """REQ-CLS-004: the ladder is told the bound, clamped to what it accepts."""
    route = mock.post(_URL).mock(return_value=httpx.Response(200, json=verdict()))

    await classifier.classify("hi", tenant="t", timeout_s=timeout_s)

    assert json.loads(route.calls.last.request.content)["options"]["latency_budget_ms"] == budget_ms


def test_the_bound_never_exceeds_the_configured_timeout(classifier: ClassifierClient) -> None:
    assert classifier.bound_s(None) == 4.0
    assert classifier.bound_s(60.0) == 4.0
    assert classifier.bound_s(1.5) == 1.5


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.ConnectError("refused"), "unavailable"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.Response(503, json={"code": "no-verdict"}), "error"),
        (httpx.Response(401, json={"code": "unauthorized"}), "error"),
        (httpx.Response(200, text="not json"), "error"),
        (httpx.Response(200, json={"sentiment": "missing everything else"}), "error"),
    ],
)
async def test_failures_raise_a_classifier_error(
    classifier: ClassifierClient, mock: respx.MockRouter, response: Any, reason: str
) -> None:
    """REQ-CLS-005: every way of not getting a verdict is one exception type."""
    if isinstance(response, Exception):
        mock.post(_URL).mock(side_effect=response)
    else:
        mock.post(_URL).mock(return_value=response)

    with pytest.raises(ClassifierError) as caught:
        await classifier.classify("hi", tenant="t", timeout_s=1.0)

    assert caught.value.reason == reason


async def test_the_key_never_renders(
    classifier: ClassifierClient, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    """REQ-CLS-008: not in the repr, the error, or any log line of a failed call."""
    mock.post(_URL).mock(return_value=httpx.Response(401, json={"detail": _KEY}))

    with caplog.at_level(logging.DEBUG), pytest.raises(ClassifierError) as caught:
        await classifier.classify("hi", tenant="t", timeout_s=1.0)

    assert _KEY not in repr(classifier)
    assert _KEY not in str(caught.value)
    assert _KEY not in caplog.text


async def test_unknown_fields_and_labels_are_tolerated(
    classifier: ClassifierClient, mock: respx.MockRouter
) -> None:
    """REQ-CLS-009: a new label upstream is a value here, not a failure."""
    mock.post(_URL).mock(
        return_value=httpx.Response(
            200,
            json=verdict(
                intent={"label": "warranty_claim", "confidence": 0.6},
                brand_new_field={"anything": True},
            ),
        )
    )

    result = await classifier.classify("hi", tenant="t", timeout_s=1.0)

    assert result.intent is not None
    assert result.intent.label == "warranty_claim"
    assert "intent=warranty_claim" in result.summary()


def test_a_label_that_is_not_a_slug_renders_as_other() -> None:
    assert label_slug("refund_request") == "refund_request"
    for hostile in ("</script>", "Refund", "a b", "x" * 41, "", "line\nbreak"):
        assert label_slug(hostile) == "other"
