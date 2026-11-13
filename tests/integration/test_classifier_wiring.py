"""The real application against a double of air-classifier's HTTP contract — spec 0005.

The unit tests mock ``/v1/classify`` loosely. This double enforces what air-classifier
itself enforces (``schemas/classify.py``, ``schemas/common.py``): ``extra="forbid"`` on
the request and on ``options``, ``latency_budget_ms`` within ``[50, 30000]``, and
``X-API-Key`` on every analysis call. A field added on this side alone fails here, not
in production. air-model-gateway is the double from ``test_model_gateway_wiring.py``,
so the turn is real end to end.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from air_orchestrator_service.config import Settings
from air_orchestrator_service.main import create_app
from tests.conftest import CLASSIFIER_BASE_URL, CUSTOMER_KEY, auth
from tests.integration.test_model_gateway_wiring import TOKEN, _client, _gateway_double
from tests.unit.test_classifier_client import verdict

CLASSIFIER_KEY = "aircls_orchestrator_test"
#: air-classifier ``ClassifyRequest`` and ``Options`` — both ``extra="forbid"``.
_REQUEST_FIELDS = {
    "text",
    "context",
    "metadata",
    "options",
    "channel",
    "user_segment",
    "subject",
    "rating",
    "rating_scale_max",
    "product_id",
    "verified_purchase",
    "title",
}
_OPTION_FIELDS = {
    "min_tier",
    "max_tier",
    "latency_budget_ms",
    "max_cost_usd",
    "include_trace",
    "redact_pii",
    "aspects",
}


def _problem(status: int, code: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"status": status, "code": code},
        headers={"Content-Type": "application/problem+json"},
    )


def _classify(request: httpx.Request) -> httpx.Response:
    if request.headers.get("X-API-Key") != CLASSIFIER_KEY:
        return _problem(401, "unauthorized")
    body: dict[str, Any] = json.loads(request.content)
    options = body.get("options", {})
    if set(body) - _REQUEST_FIELDS or set(options) - _OPTION_FIELDS:
        return _problem(422, "validation-error")
    if not isinstance(body.get("text"), str) or not body["text"]:
        return _problem(422, "validation-error")
    budget = options.get("latency_budget_ms", 8000)
    if not 50 <= budget <= 30_000:
        return _problem(422, "validation-error")
    return httpx.Response(200, json=verdict(metadata=body.get("metadata", {})))


@pytest.fixture
def doubles() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.get("http://air-infra.invalid:8080/v1/health").mock(return_value=httpx.Response(200))
        _gateway_double(router)
        router.post(f"{CLASSIFIER_BASE_URL}/v1/classify").mock(side_effect=_classify)
        yield router


async def test_a_customer_turn_is_classified_through_the_classifier(
    settings: Settings, doubles: respx.MockRouter
) -> None:
    """REQ-CLS-001, REQ-CLS-003: the double accepts exactly what this service sends."""
    wired = settings.model_copy(
        update={
            "model_gateway": settings.model_gateway.model_copy(
                update={"api_key": SecretStr(TOKEN)}
            ),
            "downstream": settings.downstream.model_copy(
                update={
                    "classifier": settings.downstream.classifier.model_copy(
                        update={
                            "enabled": True,
                            "base_url": CLASSIFIER_BASE_URL,
                            "api_key": SecretStr(CLASSIFIER_KEY),
                        }
                    )
                }
            ),
        }
    )
    async with _client(create_app(wired)) as http:
        response = await http.post(
            "/v1/chat", headers=auth(CUSTOMER_KEY), json={"message": "where is my order?"}
        )

    body = response.json()
    classify = next(s for s in body["trace"] if s["stage"] == "classify")
    assert classify["status"] == "ok", classify
    assert body["status"] == "ok"
    assert body["degraded"] == []
