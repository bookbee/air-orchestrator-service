"""The real application against a double of air-model-gateway's HTTP contract.

Unit tests patch ``ModelGatewayClient.generate`` and so cannot see a wire break — which
is precisely the break that shipped: every call 404'd on ``/v1/inference`` and, once
that was fixed, would have 422'd on ``task``, while ``/v1/ready`` stayed green. This
double enforces the rules the gateway enforces (air-model-gateway
``docs/06-consumer-migration.md``), so the same break fails here, before a deploy:

* ``/v1/inference`` is a 404;
* ``/v1/generate`` rejects any field outside ``GenerateInput`` and any ``model`` that is
  not a class;
* every route needs this service's token, except ``/v1/ready``.

Everything else is the real app: middleware, auth, the engine, the client, readiness.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from pydantic import SecretStr

from air_orchestrator_service.config import Settings
from air_orchestrator_service.main import create_app
from tests.conftest import CUSTOMER_KEY, MODEL_GATEWAY_BASE_URL, auth

TOKEN = "air-orchestrator-service-test"
GATEWAY_ANSWER = "Answered by the gateway double."
_FIELDS = {
    "model",
    "provider",
    "pin",
    "messages",
    "max_tokens",
    "temperature",
    "json_schema",
    "schema_name",
    "cache_prefix",
    "stream",
    "cache",
}
_CLASSES = {"local", "auto", "cloud"}


def _problem(status: int, code: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"type": "about:blank", "status": status, "code": code, "request_id": "gw_1"},
        headers={"Content-Type": "application/problem+json"},
    )


def _authorised(request: httpx.Request, token: str) -> bool:
    return request.headers.get("X-API-Key") == token


def _gateway_double(router: respx.MockRouter, *, token: str = TOKEN) -> None:
    base = MODEL_GATEWAY_BASE_URL

    def generate(request: httpx.Request) -> httpx.Response:
        if not _authorised(request, token):
            return _problem(401, "unauthorized")
        body: dict[str, Any] = json.loads(request.content)
        if set(body) - _FIELDS:
            return _problem(422, "validation_error")
        if body.get("model", "auto") not in _CLASSES:
            return _problem(422, "invalid_model_choice")
        return httpx.Response(
            200,
            json={
                "id": "gen_1",
                "model_class": body.get("model", "auto"),
                "model": "claude-sonnet-5",
                "provider": "anthropic",
                "content": GATEWAY_ANSWER,
                "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
                "cost_usd": 0.0001,
                "cached": False,
                "refusal": False,
                "finish_reason": "stop",
            },
        )

    def capabilities(request: httpx.Request) -> httpx.Response:
        if not _authorised(request, token):
            return _problem(401, "unauthorized")
        return httpx.Response(
            200,
            json={
                "generate": {
                    name: {"allowed": True, "available": True, "chain": []} for name in _CLASSES
                }
            },
        )

    router.post(f"{base}/v1/inference").mock(return_value=_problem(404, "not_found"))
    router.post(f"{base}/v1/generate").mock(side_effect=generate)
    router.get(f"{base}/v1/capabilities").mock(side_effect=capabilities)
    router.get(f"{base}/v1/ready").mock(
        return_value=httpx.Response(200, json={"status": "ready", "providers": {}})
    )


def _app(settings: Settings, *, token: str) -> FastAPI:
    wired = settings.model_copy(
        update={
            "model_gateway": settings.model_gateway.model_copy(update={"api_key": SecretStr(token)})
        }
    )
    return create_app(wired)


@pytest.fixture
def gateway() -> Iterator[respx.MockRouter]:
    # `assert_all_mocked`: a call to any URL the double does not define fails the test
    # rather than escaping to the network. air-infra is mocked as healthy so readiness
    # turns on the gateway alone.
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.get("http://air-infra.invalid:8080/v1/health").mock(return_value=httpx.Response(200))
        _gateway_double(router)
        yield router


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def test_a_chat_turn_is_answered_through_the_gateway(
    settings: Settings, gateway: respx.MockRouter
) -> None:
    """REQ-MGW-016 (and AC-1): the shipped failure, closed."""
    app = _app(settings, token=TOKEN)
    async with _client(app) as http:
        response = await http.post("/v1/chat", headers=auth(CUSTOMER_KEY), json={"message": "hi"})

    body = response.json()
    assert response.status_code == httpx.codes.OK
    assert body["status"] == "ok"
    assert body["answer"] == GATEWAY_ANSWER
    assert body["degraded"] == []
    assert not gateway.routes[0].called  # /v1/inference: never again


async def test_ready_is_200_when_the_gateway_serves_this_service(
    settings: Settings, gateway: respx.MockRouter
) -> None:
    app = _app(settings, token=TOKEN)
    async with _client(app) as http:
        response = await http.get("/v1/ready")

    assert response.status_code == httpx.codes.OK


async def test_ready_is_503_when_the_gateway_rejects_the_token(
    settings: Settings, gateway: respx.MockRouter
) -> None:
    """REQ-MGW-010 (AC-2): the gateway's own /v1/ready is green; ours must not be."""
    app = _app(settings, token="a-token-the-gateway-never-issued")
    async with _client(app) as http:
        ready = await http.get("/v1/ready")
        turn = await http.post("/v1/chat", headers=auth(CUSTOMER_KEY), json={"message": "hi"})

    assert ready.status_code == httpx.codes.SERVICE_UNAVAILABLE
    entry = next(d for d in ready.json()["dependencies"] if d["service"] == "air-model-gateway")
    assert "token" in entry["detail"]
    assert "a-token-the-gateway-never-issued" not in ready.text
    # And a turn still answers, degraded, naming the gateway (Article III).
    assert turn.status_code == httpx.codes.OK
    assert turn.json()["degraded"] == ["air-model-gateway"]
