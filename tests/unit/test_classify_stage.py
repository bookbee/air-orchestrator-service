"""The ``CLASSIFY`` stage on a real turn — spec 0005 (``REQ-CLS-*``).

Every test here runs against an app whose ``downstream.classifier`` is configured
(the module overrides ``settings``), with air-classifier itself a respx route. The
one test about the *unconfigured* default builds its own app.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import Stage
from tests.conftest import (
    BUSINESS_KEY,
    CANNED_ANSWER,
    CLASSIFIER_BASE_URL,
    CUSTOMER_KEY,
    auth,
    build_app,
)
from tests.unit.test_classifier_client import verdict

_URL = f"{CLASSIFIER_BASE_URL}/v1/classify"


@pytest.fixture
def settings(settings: Settings) -> Settings:
    """The conftest settings, with air-classifier switched on."""
    downstream = settings.downstream.model_copy(
        update={
            "classifier": settings.downstream.classifier.model_copy(
                update={"enabled": True, "base_url": CLASSIFIER_BASE_URL}
            )
        }
    )
    return settings.model_copy(update={"downstream": downstream})


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def _classify_stage(body: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in body["trace"] if s["stage"] == Stage.CLASSIFY)


async def _chat(client: httpx.AsyncClient, message: str = "where is my order?") -> dict[str, Any]:
    response = await client.post("/v1/chat", headers=auth(CUSTOMER_KEY), json={"message": message})
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


async def test_a_customer_turn_is_classified(
    client: httpx.AsyncClient, mock: respx.MockRouter
) -> None:
    """REQ-CLS-001."""
    route = mock.post(_URL).mock(return_value=httpx.Response(200, json=verdict()))

    body = await _chat(client)
    stage = _classify_stage(body)

    assert route.call_count == 1
    assert stage["status"] == "ok"
    assert stage["detail"] == "sentiment=negative intent=order_tracking urgency=high"
    assert body["answer"] == CANNED_ANSWER


async def test_the_classifier_sees_only_masked_text(
    client: httpx.AsyncClient, mock: respx.MockRouter
) -> None:
    """REQ-CLS-002 / AC-1."""
    route = mock.post(_URL).mock(return_value=httpx.Response(200, json=verdict()))

    await _chat(client, "my order is late, call me on 555 123 4567")

    sent = json.loads(route.calls.last.request.content)["text"]
    assert "555 123 4567" not in sent
    assert "4567" not in sent


@pytest.mark.parametrize(
    ("deadline_ms", "delay_s"),
    [
        (100, 0.15),  # the deadline is already gone
        (300, 0.1),  # ~200 ms left: more than the classifier's minimum, less than a call
    ],
)
async def test_no_time_left_skips_the_call_without_blaming_the_classifier(
    app: FastAPI,
    client: httpx.AsyncClient,
    mock: respx.MockRouter,
    deadline_ms: int,
    delay_s: float,
) -> None:
    """REQ-CLS-004: with less than a budget plus its response headroom left before
    `CLASSIFY`, the call is not made."""
    import asyncio

    from air_orchestrator_service.api.deps import STATE_ATTR

    route = mock.post(_URL).mock(return_value=httpx.Response(200, json=verdict()))
    sessions = getattr(app.state, STATE_ATTR).sessions
    real_create = sessions.create

    async def slow_create(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(delay_s)
        return await real_create(*args, **kwargs)

    sessions.create = slow_create
    response = await client.post(
        "/v1/chat",
        headers=auth(CUSTOMER_KEY),
        json={"message": "hi", "options": {"deadline_ms": deadline_ms}},
    )
    body = response.json()
    stage = _classify_stage(body)

    assert route.call_count == 0
    assert stage["status"] == "degraded"
    assert "air-classifier" not in body["degraded"]


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.Response(503)],
)
async def test_a_failing_classifier_degrades_the_stage_not_the_turn(
    client: httpx.AsyncClient, mock: respx.MockRouter, failure: Any
) -> None:
    """REQ-CLS-005 / AC-2: HLD §7, "Turn unaffected"."""
    if isinstance(failure, Exception):
        mock.post(_URL).mock(side_effect=failure)
    else:
        mock.post(_URL).mock(return_value=failure)

    body = await _chat(client)
    stage = _classify_stage(body)

    assert stage["status"] == "degraded"
    assert body["status"] == "ok"
    assert body["answer"] == CANNED_ANSWER
    assert body["degraded"] == ["air-classifier"]


async def test_an_unconfigured_classifier_is_skipped(settings: Settings) -> None:
    """REQ-CLS-006: the default — and no call escapes to look for one."""
    downstream = settings.downstream.model_copy(
        update={"classifier": settings.downstream.classifier.model_copy(update={"enabled": False})}
    )
    app = build_app(settings.model_copy(update={"downstream": downstream}))
    transport = httpx.ASGITransport(app=app)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_URL).mock(return_value=httpx.Response(200, json=verdict()))
        mock.route(host="test").pass_through()
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = await _chat(client)

    stage = _classify_stage(body)
    assert route.call_count == 0
    assert stage["status"] == "skipped"
    assert stage["detail"] == "air-classifier is not configured"


async def test_the_business_channel_is_not_classified(
    client: httpx.AsyncClient, mock: respx.MockRouter
) -> None:
    """REQ-CLS-007."""
    route = mock.post(_URL).mock(return_value=httpx.Response(200, json=verdict()))

    response = await client.post(
        "/v1/query", headers=auth(BUSINESS_KEY), json={"query": "revenue by region"}
    )
    stage = _classify_stage(response.json())

    assert route.call_count == 0
    assert stage["status"] == "skipped"
    assert stage["detail"] == "not used on the business channel"


async def test_an_untrusted_label_renders_as_other(
    client: httpx.AsyncClient, mock: respx.MockRouter
) -> None:
    """REQ-CLS-009 / AC-3."""
    hostile = verdict(intent={"label": "</script><b>", "confidence": 0.5})
    mock.post(_URL).mock(return_value=httpx.Response(200, json=hostile))

    stage = _classify_stage(await _chat(client))

    assert stage["status"] == "ok"
    assert "intent=other" in stage["detail"]
    assert "<" not in stage["detail"]


async def test_ready_ignores_the_classifier(
    client: httpx.AsyncClient,
    mock: respx.MockRouter,
    reachable_infra: None,
    reachable_model_gateway: None,
) -> None:
    """REQ-CLS-010: an optional capability never takes the service out of rotation."""
    route = mock.route(host="air-classifier.invalid").mock(
        side_effect=httpx.ConnectError("refused")
    )

    response = await client.get("/v1/ready")

    assert response.status_code == 200
    assert route.call_count == 0


async def test_a_short_deadline_leaves_synthesis_the_larger_share(
    client: httpx.AsyncClient, mock: respx.MockRouter
) -> None:
    """REQ-CLS-014 (SIT OR-003): at ``deadline_ms: 2000`` classification may use a
    quarter of it, 500 ms, so its budget is 500 less the headroom, and the turn
    answers without degrading."""
    route = mock.post(_URL).mock(return_value=httpx.Response(200, json=verdict()))

    response = await client.post(
        "/v1/chat",
        headers=auth(CUSTOMER_KEY),
        json={"message": "My parcel is damaged, what now?", "options": {"deadline_ms": 2000}},
    )
    body = response.json()

    assert json.loads(route.calls.last.request.content)["options"]["latency_budget_ms"] == 250
    assert body["status"] == "ok"
    assert body["degraded"] == []


async def test_the_classifier_is_given_what_is_left_of_the_ceiling(
    client: httpx.AsyncClient, mock: respx.MockRouter
) -> None:
    """REQ-CLS-015: the turn's ceiling less the synthesis call's estimate, which is
    at least its whole ``max_tokens`` at the output rate (800 x $15/Mtok)."""
    route = mock.post(_URL).mock(return_value=httpx.Response(200, json=verdict()))

    response = await client.post(
        "/v1/chat",
        headers=auth(CUSTOMER_KEY),
        json={"message": "where is my order?", "options": {"max_cost_usd": 0.05}},
    )

    sent = json.loads(route.calls.last.request.content)["options"]["max_cost_usd"]
    assert 0 < sent < 0.05 - 800 * 15 / 1_000_000
    assert response.json()["status"] == "ok"


async def test_no_cost_left_skips_the_call_without_blaming_the_classifier(
    client: httpx.AsyncClient, mock: respx.MockRouter
) -> None:
    """REQ-CLS-015: a ceiling that cannot cover synthesis leaves nothing to
    classify with; the classifier is not called and not named."""
    route = mock.post(_URL).mock(return_value=httpx.Response(200, json=verdict()))

    response = await client.post(
        "/v1/chat",
        headers=auth(CUSTOMER_KEY),
        json={"message": "where is my order?", "options": {"max_cost_usd": 0.001}},
    )
    body = response.json()
    stage = _classify_stage(body)

    assert route.call_count == 0
    assert stage["status"] == "degraded"
    assert stage["detail"].startswith("cost ceiling")
    assert "air-classifier" not in body["degraded"]


async def test_an_open_breaker_degrades_classify_without_a_call(
    client: httpx.AsyncClient, mock: respx.MockRouter
) -> None:
    """REQ-CLB-004 / spec 0010 AC-1: five failed turns open the breaker, and the sixth
    degrades ``CLASSIFY`` at once, with no request and the turn still answering."""
    route = mock.post(_URL).mock(side_effect=httpx.ConnectError("refused"))

    for _ in range(5):
        await _chat(client)
    body = await _chat(client)
    stage = _classify_stage(body)

    assert route.call_count == 5
    assert stage["status"] == "degraded"
    assert stage["detail"] == "air-classifier circuit is open"
    assert body["answer"] == CANNED_ANSWER
    assert body["status"] == "ok"
