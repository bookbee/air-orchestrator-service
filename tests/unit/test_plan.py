"""The planner seam (spec 0012 T5): today's rule, moved, and an injected plan."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from air_orchestrator_service.constants import Channel, Route
from air_orchestrator_service.engine.plan import Plan, PlanContext, RulePlanner, Step
from air_orchestrator_service.schemas.common import Principal
from tests.conftest import CUSTOMER_KEY, FixedPlanner, auth, use_planner


def _principal(channel: Channel, *, allow_actions: bool = True) -> Principal:
    return Principal(key_id="k", name="k", channel=channel, tenant="t", allow_actions=allow_actions)


_RAG_REASON = "customer message: answer from the knowledge base"
_PROPOSE_REASON = "'/propose' present, exercising the proposal path"

_CASES = [
    # channel, rag serves, text, confirming, allow_actions -> route, reason, retrieve
    (Channel.CUSTOMER, True, "hello", False, True, Route.RAG, _RAG_REASON, True),
    (Channel.CUSTOMER, False, "hello", False, True, Route.DIRECT, "direct answer", False),
    (Channel.BUSINESS, True, "hello", False, True, Route.DIRECT, "direct answer", False),
    (Channel.BUSINESS, False, "hello", False, True, Route.DIRECT, "direct answer", False),
    (
        Channel.CUSTOMER,
        True,
        "please /PROPOSE x",
        False,
        True,
        Route.ACTION,
        _PROPOSE_REASON,
        False,
    ),
    (Channel.BUSINESS, False, "/propose", False, True, Route.ACTION, _PROPOSE_REASON, False),
    (Channel.CUSTOMER, True, "/propose", False, False, Route.RAG, _RAG_REASON, True),
    (Channel.CUSTOMER, False, "/propose", False, False, Route.DIRECT, "direct answer", False),
    (Channel.CUSTOMER, True, "/propose", True, True, Route.DIRECT, "direct answer", False),
    (Channel.CUSTOMER, True, "hello", True, True, Route.DIRECT, "direct answer", False),
]


@pytest.mark.parametrize(
    ("channel", "rag", "text", "confirming", "allow", "route", "reason", "retrieve"), _CASES
)
async def test_the_rule_planner_reproduces_todays_routes(
    channel: Channel,
    rag: bool,
    text: str,
    confirming: bool,
    allow: bool,
    route: Route,
    reason: str,
    retrieve: bool,
) -> None:
    """The routing block that lived in ``TurnEngine._pipeline``, case by case — and,
    on every input, no air-tools-gateway step (Article V)."""
    plan = await RulePlanner().plan(
        PlanContext(
            text=text,
            principal=_principal(channel, allow_actions=allow),
            confirming=confirming,
            rag_serves=rag,
        )
    )

    assert plan.routes == (route,)
    assert plan.reason == reason
    assert plan.retrieve is retrieve
    assert plan.stand_in_proposal is (route is Route.ACTION)
    assert plan.reads == () and plan.write is None


def test_the_route_event_names_only_what_the_plan_uses() -> None:
    plan = Plan(
        routes=(Route.TOOLS,),
        reason="r",
        retrieve=True,
        reads=(Step("orders.list_recent"),),
    )

    assert plan.capabilities == ["air-rag", "orders.list_recent"]


async def test_an_injected_plan_decides_the_route(app: FastAPI) -> None:
    """The seam T6 to T8 are driven through: the engine reports the injected plan's
    routes and reason, and gives the planner the masked text and the principal."""
    planner = FixedPlanner(Plan(routes=(Route.DIRECT,), reason="injected for a test"))
    use_planner(app, planner)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            "/v1/chat", headers=auth(CUSTOMER_KEY), json={"message": "/propose please"}
        )

    assert response.status_code == httpx.codes.OK, response.text
    assert response.json()["routes"] == ["direct"]
    assert response.json()["proposal"] is None
    [seen] = planner.seen
    assert seen.principal.key_id == "customer"
    assert seen.confirming is False
    assert "/propose please" in seen.text
