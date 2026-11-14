"""The turn cost ceiling, enforced before the money is spent — spec 0008 (``REQ-COST-*``).

SIT bug OR-002: a turn with ``max_cost_usd: 0.0001`` spent $0.0031 and said ``ok``.
These tests drive the real app with a gateway stub that records each call and
prices it, so "no call was made" is observed, not inferred from a status.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from air_orchestrator_service.api.deps import STATE_ATTR
from air_orchestrator_service.clients.model_gateway import GenerateResult, GenerateUsage
from air_orchestrator_service.config import Settings
from air_orchestrator_service.engine.turn import TurnEngine
from tests.conftest import (
    BUSINESS_KEY,
    CLASSIFIER_BASE_URL,
    CUSTOMER_KEY,
    auth,
    build_app,
)
from tests.unit.test_classifier_client import verdict

_CLASSIFY_URL = f"{CLASSIFIER_BASE_URL}/v1/classify"


def _priced_gateway(app: FastAPI, *costs: float) -> list[dict[str, Any]]:
    """Replace ``generate`` with one that records each call and charges the next
    of ``costs`` (the last one repeats)."""
    calls: list[dict[str, Any]] = []

    async def generate(**kwargs: Any) -> GenerateResult:
        calls.append(kwargs)
        cost = costs[min(len(calls), len(costs)) - 1]
        content = '{"answer": "mock"}' if kwargs.get("json_schema") else "An answer."
        return GenerateResult(
            content=content,
            cost_usd=cost,
            finish_reason="stop",
            usage=GenerateUsage(prompt_tokens=10, completion_tokens=5),
        )

    getattr(app.state, STATE_ATTR).model_gateway.generate = generate
    return calls


def _turn(channel: str, **options: Any) -> tuple[str, str, dict[str, Any]]:
    if channel == "customer":
        return "/v1/chat", CUSTOMER_KEY, {"message": "Tell me about returns", "options": options}
    return "/v1/query", BUSINESS_KEY, {"query": "Weekly return rate?", "options": options}


def _synthesise(body: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in body["trace"] if s["stage"] == "synthesise")


@pytest.mark.parametrize("channel", ["customer", "business"])
async def test_a_prose_call_over_the_ceiling_is_not_made(
    app: FastAPI, client: httpx.AsyncClient, channel: str
) -> None:
    """REQ-COST-002, the SIT repro, on both channels."""
    calls = _priced_gateway(app, 0.003)
    path, key, body = _turn(channel, max_cost_usd=0.0001)

    response = await client.post(path, headers=auth(key), json=body)
    result = response.json()

    assert response.status_code == 200
    assert calls == []
    assert result["status"] == "degraded"
    assert result["degraded"] == []
    assert _synthesise(result)["status"] == "degraded"
    assert "cost ceiling" in _synthesise(result)["detail"]
    assert result["usage"]["cost_usd"] == 0


async def test_a_caller_cannot_raise_the_ceiling(settings: Settings) -> None:
    """REQ-COST-001: the operator's ceiling holds whatever the caller asks for."""
    turn = settings.turn.model_copy(update={"max_cost_usd": 0.0001})
    app = build_app(settings.model_copy(update={"turn": turn}))
    calls = _priced_gateway(app, 0.003)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        path, key, body = _turn("customer", max_cost_usd=10)
        result = (await client.post(path, headers=auth(key), json=body)).json()

    assert calls == []
    assert result["status"] == "degraded"


async def test_a_structured_call_over_the_ceiling_is_not_made(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """REQ-COST-003: the prose call fits and spends $0.02; the structured call
    would need about $0.012 more than the $0.025 ceiling leaves. The answer was
    given, so only `structured` narrows."""
    calls = _priced_gateway(app, 0.02)
    path, key, body = _turn("business", max_cost_usd=0.025)
    body["output_schema"] = {"type": "object", "properties": {"rate": {"type": "number"}}}

    result = (await client.post(path, headers=auth(key), json=body)).json()

    assert len(calls) == 1
    assert "json_schema" not in calls[0] or calls[0]["json_schema"] is None
    assert result["structured"] == {"answer": "An answer."}
    assert result["status"] == "ok"
    assert result["usage"]["cost_usd"] == 0.02


def test_the_estimate_is_an_upper_bound_for_the_configured_rates(
    app: FastAPI, settings: Settings
) -> None:
    """REQ-COST-004: 3,000 characters at 3 per token is 1,000 prompt tokens at
    $3/Mtok, plus all 800 output tokens at $15/Mtok. A real call on that prompt
    (about 750 tokens of English) that used every output token costs less."""
    state = getattr(app.state, STATE_ATTR)
    engine = TurnEngine(settings, state.sessions, state.model_gateway)
    messages = [{"role": "system", "content": "x" * 1000}, {"role": "user", "content": "y" * 2000}]

    estimate = engine._estimate(messages, 800)

    assert estimate == pytest.approx(1000 * 3 / 1e6 + 800 * 15 / 1e6)
    assert estimate >= 750 * 3 / 1e6 + 800 * 15 / 1e6


# ── The classifier's spend (REQ-COST-005/006, SIT OR-003) ─────────────────────


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def classified_app(settings: Settings) -> FastAPI:
    classifier = settings.downstream.classifier.model_copy(
        update={"enabled": True, "base_url": CLASSIFIER_BASE_URL}
    )
    downstream = settings.downstream.model_copy(update={"classifier": classifier})
    return build_app(settings.model_copy(update={"downstream": downstream}))


def _classifier_spends(mock: respx.MockRouter, cost: float) -> None:
    usage = {"tiers_run": 4, "tokens_in": 300, "tokens_out": 40, "est_cost_usd": cost}
    mock.post(_CLASSIFY_URL).mock(return_value=httpx.Response(200, json=verdict(usage=usage)))


async def test_classifier_spend_is_counted_in_usage_and_the_session(
    classified_app: FastAPI, mock: respx.MockRouter
) -> None:
    """REQ-COST-005."""
    _classifier_spends(mock, 0.004)
    _priced_gateway(classified_app, 0.001)

    transport = httpx.ASGITransport(app=classified_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        path, key, body = _turn("customer")
        result = (await client.post(path, headers=auth(key), json=body)).json()
        view = await client.get(f"/v1/sessions/{result['session_id']}", headers=auth(key))

    assert result["usage"]["classifier_cost_usd"] == 0.004
    assert result["usage"]["cost_usd"] == pytest.approx(0.005)
    assert view.json()["total_cost_usd"] == pytest.approx(0.005)


async def test_classifier_spend_counts_toward_the_synthesis_check(
    classified_app: FastAPI, mock: respx.MockRouter
) -> None:
    """REQ-COST-006: the classifier overspent the cap it was given (as SIT found
    it can, CL-001); what is left no longer covers synthesis, so it is not made."""
    _classifier_spends(mock, 0.04)
    calls = _priced_gateway(classified_app, 0.001)

    transport = httpx.ASGITransport(app=classified_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        path, key, body = _turn("customer", max_cost_usd=0.05)
        result = (await client.post(path, headers=auth(key), json=body)).json()

    assert calls == []
    assert result["status"] == "degraded"
    assert result["usage"]["cost_usd"] == pytest.approx(0.04)
    assert "cost ceiling" in _synthesise(result)["detail"]
