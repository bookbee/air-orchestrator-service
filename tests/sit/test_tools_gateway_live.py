"""The contract double's flows, replayed against a running air-tools-gateway — spec 0012 T11.

``tests/integration/test_tools_gateway_contract.py`` proves this service against a double
of the contract. This module sends the same requests to air-tools-gateway's own build,
so the two implementations of the contract are checked against each other: the gateway
refuses what the double refuses, and a real-app lookup, proposal and confirmed execute
go through it.

**Skipped unless ``AIR_TOOLS_GATEWAY_SIT_URL`` is set** (``make sit-tools``), so ``make
check`` stays hermetic. It uses the gateway's published development keys and fixture
customer ``cust_1001`` (air-tools-gateway's README). The execute flow really cancels an
order in the gateway's in-memory fixtures; it picks a cancellable one from a live lookup
and is skipped when none is left. Restarting the gateway resets them.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from air_orchestrator_service.api.deps import require_principal
from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import Channel, Route, Stage
from air_orchestrator_service.engine.plan import Plan, Step
from air_orchestrator_service.schemas.common import Principal
from tests.conftest import CUSTOMER_KEY, FixedPlanner, auth, build_app, use_planner
from tests.unit.test_gather_stage import _gateway

_URL = os.environ.get("AIR_TOOLS_GATEWAY_SIT_URL", "")
_READ = "airtg_read_local_dev"
_EXECUTE = "airtg_exec_local_dev"
_SUBJECT = "cust_1001"

pytestmark = [
    pytest.mark.sit,
    pytest.mark.skipif(
        not _URL, reason="set AIR_TOOLS_GATEWAY_SIT_URL to run against a live gateway"
    ),
]


@pytest.fixture
def settings(settings: Settings) -> Settings:
    gateway = settings.downstream.tools_gateway.model_validate(
        {
            "enabled": True,
            "base_url": _URL,
            "tenant_keys": {"tenant-a": {"read": _READ, "execute": _EXECUTE}},
            "allow": {"customer": ["orders.*"]},
        }
    )
    downstream = settings.downstream.model_copy(update={"tools_gateway": gateway})
    return settings.model_copy(update={"downstream": downstream})


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    built = build_app(settings)

    async def principal() -> Principal:
        return Principal(
            key_id="customer",
            name="sit",
            channel=Channel.CUSTOMER,
            tenant="tenant-a",
            scopes=frozenset({"chat:write", "session:read"}),
            allow_actions=True,
            subject=_SUBJECT,
        )

    built.dependency_overrides[require_principal] = principal
    return built


async def _chat(app: FastAPI, plan: Plan, **body: Any) -> dict[str, Any]:
    use_planner(app, FixedPlanner(plan))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            "/v1/chat", headers=auth(CUSTOMER_KEY), json={"message": "my order", **body}
        )
    assert response.status_code == httpx.codes.OK, response.text
    result: dict[str, Any] = response.json()
    return result


def _gather(body: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in body["trace"] if s["stage"] == Stage.GATHER)


async def _cancellable() -> str | None:
    async with httpx.AsyncClient(base_url=_URL) as http:
        response = await http.post(
            "/v1/tools/orders.list_recent",
            headers={"X-API-Key": _READ},
            json={"arguments": {"limit": 10}, "subject": _SUBJECT},
        )
    response.raise_for_status()
    orders = response.json()["data"]["orders"]
    return next((o["order_id"] for o in orders if o.get("cancellable")), None)


async def test_the_live_gateway_refuses_what_the_double_refuses() -> None:
    """The double's key-scope and body rules, as the real gateway answers them."""
    lookup = "/v1/tools/orders.list_recent"
    execute = "/v1/actions/orders.cancel/execute"
    idem = {"Idempotency-Key": "airo-prop_sit00000000"}
    good = {"arguments": {"limit": 1}, "subject": _SUBJECT}
    async with httpx.AsyncClient(base_url=_URL) as http:
        cases = [
            (
                "unknown key",
                await http.post(lookup, json=good, headers={"X-API-Key": "airtg_nope"}),
                401,
            ),
            (
                "an extra field",
                await http.post(lookup, json={**good, "tenant": "x"}, headers={"X-API-Key": _READ}),
                400,
            ),
            (
                "the execute key on a lookup",
                await http.post(lookup, json=good, headers={"X-API-Key": _EXECUTE}),
                403,
            ),
            (
                "the execute key on the inventory",
                await http.get("/v1/capabilities", headers={"X-API-Key": _EXECUTE}),
                403,
            ),
            (
                "the read key on execute",
                await http.post(
                    execute,
                    json={"proposal_id": "gp_x", "subject": _SUBJECT},
                    headers={"X-API-Key": _READ, **idem},
                ),
                403,
            ),
        ]
    for what, response, status in cases:
        assert response.status_code == status, (what, response.text)


async def test_a_lookup_turn_through_the_live_gateway(app: FastAPI) -> None:
    calls = _gateway(app)
    plan = Plan(
        routes=(Route.TOOLS,), reason="sit", reads=(Step("orders.list_recent", {"limit": 5}),)
    )

    body = await _chat(app, plan)

    assert _gather(body)["detail"] == "orders.list_recent ok", body["trace"]
    assert body["status"] == "ok"
    [call] = calls
    assert "48213" in json.dumps(call["messages"])  # a fixture order of cust_1001


async def test_a_proposal_and_its_execution_through_the_live_gateway(app: FastAPI) -> None:
    _gateway(app)
    order = await _cancellable()
    if order is None:
        pytest.skip("no cancellable order left for cust_1001: restart air-tools-gateway")
    cancel = Step("orders.cancel", {"order_id": order})

    proposed = await _chat(app, Plan(routes=(Route.ACTION,), reason="sit", write=cancel))
    assert proposed["proposal"] is not None, proposed["trace"]
    assert _gather(proposed)["detail"] == "orders.cancel proposed"

    confirmed = await _chat(
        app,
        Plan(routes=(Route.DIRECT,), reason="direct answer"),
        session_id=proposed["session_id"],
        confirm={"proposal_id": proposed["proposal"]["proposal_id"], "approve": True},
    )

    assert _gather(confirmed)["detail"] == "orders.cancel done", confirmed["trace"]
    assert confirmed["answer"].startswith("Done")
    assert await _cancellable() != order
