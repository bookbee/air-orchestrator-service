"""The real application against a double of air-tools-gateway's contract — spec 0012.

``tools_gateway_double.py`` holds every request to ``contract.openapi.yaml``. Here,
one hand-made request per rule shows the double refuses what the contract refuses
(``REQ-TGW-024``), and then a real-app turn for each flow (lookup, propose, execute)
goes through it with nothing refused. A field added on this side alone, or a key sent
to the wrong endpoint, fails here rather than in production.

The plan is injected with ``FixedPlanner`` and the principal with a verified subject,
as in the unit tests, until specs 0013 and 0014 land.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from air_orchestrator_service.api.deps import require_principal
from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import Channel, Route, Stage
from air_orchestrator_service.engine.plan import Plan, Step
from air_orchestrator_service.schemas.common import Principal
from tests.conftest import (
    CUSTOMER_KEY,
    TOOLS_GATEWAY_BASE_URL,
    FixedPlanner,
    auth,
    build_app,
    use_planner,
)
from tests.integration.tools_gateway_double import ToolsGatewayDouble
from tests.unit.test_gather_stage import _gateway

_READ = "airtg_read_contract_test"
_EXECUTE = "airtg_exec_contract_test"
_OTHER_READ = "airtg_read_other_tenant"
_OTHER_EXECUTE = "airtg_exec_other_tenant"
_SUBJECT = "cust_123"
_CANCEL = Step("orders.cancel", {"order_id": "55120", "reason": "changed_mind"})


@pytest.fixture
def double() -> Iterator[ToolsGatewayDouble]:
    gateway = ToolsGatewayDouble(
        base_url=TOOLS_GATEWAY_BASE_URL,
        keys={"tenant-a": (_READ, _EXECUTE), "tenant-b": (_OTHER_READ, _OTHER_EXECUTE)},
    )
    with respx.mock(assert_all_called=False) as router:
        gateway.mount(router)
        yield gateway


@pytest.fixture
def settings(settings: Settings) -> Settings:
    gateway = settings.downstream.tools_gateway.model_validate(
        {
            "enabled": True,
            "base_url": TOOLS_GATEWAY_BASE_URL,
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
            name="test",
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


def _calls(double: ToolsGatewayDouble) -> list[tuple[str, str]]:
    return [(r.method, r.url.path) for r in double.requests]


# ── The double refuses what the contract refuses ─────────────────────────────


async def test_the_double_refuses_what_the_contract_refuses(double: ToolsGatewayDouble) -> None:
    """``REQ-TGW-024``. One hand-made request per rule, each answered as the contract
    says the real gateway would, and each recorded as a refusal."""
    lookup = "/v1/tools/orders.list_recent"
    propose = "/v1/actions/orders.cancel/propose"
    execute = "/v1/actions/orders.cancel/execute"
    good = {"arguments": {"order_id": "55120"}, "subject": _SUBJECT}

    async with httpx.AsyncClient(base_url=TOOLS_GATEWAY_BASE_URL) as http:

        async def post(path: str, body: Any, key: str = _READ, **headers: str) -> httpx.Response:
            return await http.post(path, json=body, headers={"X-API-Key": key, **headers})

        made = await post(propose, good)
        assert made.status_code == 200, made.text
        gp = made.json()["proposal_id"]
        executes = {"proposal_id": gp, "subject": _SUBJECT}
        cases: list[tuple[str, httpx.Response, int, str]] = [
            ("no key", await http.get("/v1/capabilities"), 401, "unauthorized"),
            ("unknown key", await post(lookup, good, key="airtg_nope"), 401, "unauthorized"),
            (
                "an extra field",
                await post(lookup, {"arguments": {}, "subject": _SUBJECT, "tenant": "x"}),
                400,
                "invalid_arguments",
            ),
            (
                "arguments outside the input_schema",
                await post(lookup, {"arguments": {"limit": 50}, "subject": _SUBJECT}),
                400,
                "invalid_arguments",
            ),
            (
                "no subject where one is required",
                await post(lookup, {"arguments": {}, "subject": None}),
                400,
                "invalid_arguments",
            ),
            (
                "a write run as a lookup",
                await post("/v1/tools/orders.cancel", good),
                404,
                "unknown_capability",
            ),
            (
                "the execute key on the inventory",
                await http.get("/v1/capabilities", headers={"X-API-Key": _EXECUTE}),
                403,
                "forbidden",
            ),
            (
                "the execute key on a lookup",
                await post(lookup, {"arguments": {}, "subject": _SUBJECT}, key=_EXECUTE),
                403,
                "forbidden",
            ),
            (
                "the execute key on propose",
                await post(propose, good, key=_EXECUTE),
                403,
                "forbidden",
            ),
            (
                "the read key on execute",
                await post(execute, executes, **{"Idempotency-Key": "airo-prop_000000000001"}),
                403,
                "forbidden",
            ),
            (
                "no Idempotency-Key",
                await post(execute, executes, key=_EXECUTE),
                400,
                "invalid_arguments",
            ),
            (
                "an extra field on execute",
                await post(
                    execute,
                    {**executes, "arguments": {}},
                    key=_EXECUTE,
                    **{"Idempotency-Key": "airo-prop_000000000001"},
                ),
                400,
                "invalid_arguments",
            ),
            (
                "another customer's proposal",
                await post(
                    execute,
                    {"proposal_id": gp, "subject": "cust_456"},
                    key=_EXECUTE,
                    **{"Idempotency-Key": "airo-prop_000000000001"},
                ),
                404,
                "not_found",
            ),
            (
                "another tenant's proposal",
                await post(
                    execute,
                    executes,
                    key=_OTHER_EXECUTE,
                    **{"Idempotency-Key": "airo-prop_000000000001"},
                ),
                404,
                "not_found",
            ),
        ]
        first = await post(
            execute, executes, key=_EXECUTE, **{"Idempotency-Key": "airo-prop_000000000001"}
        )
        replay = await post(
            execute, executes, key=_EXECUTE, **{"Idempotency-Key": "airo-prop_000000000001"}
        )
        second_key = await post(
            execute, executes, key=_EXECUTE, **{"Idempotency-Key": "airo-prop_000000000002"}
        )

    for what, response, status, code in cases:
        assert (response.status_code, response.json()["code"]) == (status, code), what
        assert response.headers["Content-Type"] == "application/problem+json", what
    assert first.status_code == 200 and first.json()["status"] == "done"
    assert replay.json() == first.json()
    assert (second_key.status_code, second_key.json()["code"]) == (409, "already_executed")
    assert len(double.refused) == len(cases) + 1


# ── Real turns through the double ────────────────────────────────────────────


async def test_a_lookup_turn_passes_the_contract(app: FastAPI, double: ToolsGatewayDouble) -> None:
    """The inventory and a lookup, on the read key, with nothing refused; the
    contract's example result reaches the prompt."""
    calls = _gateway(app)
    plan = Plan(
        routes=(Route.TOOLS,),
        reason="injected",
        reads=(Step("orders.list_recent", {"limit": 5}),),
    )

    body = await _chat(app, plan)

    assert double.refused == []
    assert _calls(double) == [("GET", "/v1/capabilities"), ("POST", "/v1/tools/orders.list_recent")]
    assert _gather(body)["detail"] == "orders.list_recent ok"
    assert body["status"] == "ok"
    [call] = calls
    assert "55120" in json.dumps(call["messages"])


async def test_a_proposing_turn_passes_the_contract(
    app: FastAPI, double: ToolsGatewayDouble
) -> None:
    """The inventory and a propose, on the read key, with nothing refused, and no
    execute."""
    _gateway(app)

    body = await _chat(app, Plan(routes=(Route.ACTION,), reason="injected", write=_CANCEL))

    assert double.refused == []
    assert _calls(double) == [
        ("GET", "/v1/capabilities"),
        ("POST", "/v1/actions/orders.cancel/propose"),
    ]
    assert body["proposal"]["proposal_id"].startswith("prop_")
    assert body["answer"].startswith("Cancel order 55120")


async def test_a_confirmed_turn_executes_through_the_contract(
    app: FastAPI, double: ToolsGatewayDouble
) -> None:
    """Propose, then confirm: one execute on the execute key, with an idempotency key
    the contract accepts and the subject the proposal was made for. Nothing refused."""
    _gateway(app)
    proposed = await _chat(app, Plan(routes=(Route.ACTION,), reason="injected", write=_CANCEL))

    confirmed = await _chat(
        app,
        Plan(routes=(Route.DIRECT,), reason="direct answer"),
        session_id=proposed["session_id"],
        confirm={"proposal_id": proposed["proposal"]["proposal_id"], "approve": True},
    )

    assert double.refused == []
    [execute] = [r for r in double.requests if r.url.path.endswith("/execute")]
    assert execute.headers["X-API-Key"] == _EXECUTE
    assert confirmed["status"] == "ok"
    assert confirmed["answer"].startswith("Done")
    assert _gather(confirmed)["detail"] == "orders.cancel done"
