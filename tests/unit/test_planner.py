"""The model planner — spec 0013.

Every turn runs the full app with ``planner.enabled``, so ``create_app`` builds the
``ModelPlanner`` itself, over the same ``ModelGatewayClient`` the answer uses. That
client's ``generate`` is replaced by :class:`Model`, which scripts the planning calls
(``schema_name="plan"``) one by one and records every call's messages, schema, model
class and timeout. air-tools-gateway is a respx double, and the principal is injected
with a verified subject unless a test drives the real ``air-token`` path. So "one
planning call", "never sent" and "not in the prompt" are observed, not inferred.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from air_orchestrator_service.api.deps import STATE_ATTR, require_principal
from air_orchestrator_service.clients.model_gateway import (
    GenerateResult,
    GenerateUsage,
    ModelGatewayClient,
    ModelGatewayError,
)
from air_orchestrator_service.clients.tools_gateway import Inventory, _check_entry
from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import Channel, ModelClass, Route, Stage
from air_orchestrator_service.engine.plan import ModelPlanner, PlanContext, planning_messages
from air_orchestrator_service.engine.planner_schema import (
    follow_up_schema,
    parse_follow_up,
    parse_plan,
    plan_schema,
    step_names,
)
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.prompts.registry import PromptRegistry
from air_orchestrator_service.schemas.common import Principal
from tests.conftest import (
    CUSTOMER_KEY,
    MODEL_GATEWAY_BASE_URL,
    TOOLS_GATEWAY_BASE_URL,
    auth,
    build_app,
)
from tests.unit._customer_token_support import configured_key, mint
from tests.unit._tools_gateway_support import (
    EXECUTE_A,
    INVENTORY_URL,
    READ_A,
    inventory_body,
    read_entry,
    write_entry,
)
from tests.unit.test_events import parse_sse

_TOOLS = f"{TOOLS_GATEWAY_BASE_URL}/v1/tools"
_ACTIONS = f"{TOOLS_GATEWAY_BASE_URL}/v1/actions"
_SUBJECT = "cust_1001"
_TENANT = "tenant-a"
_ANSWER = "Your order 48213 is on its way."
_CATALOGUE_MARKER = "CATALOGUE-ONLY-DESCRIPTION"


def _get_entry() -> dict[str, Any]:
    return read_entry(
        "orders.get",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["order_id"],
            "properties": {"order_id": {"type": "string", "pattern": "^[0-9]{4,12}$"}},
        },
    )


_ENTRIES = [
    read_entry("orders.list_recent", description=f"Recent orders. {_CATALOGUE_MARKER}"),
    _get_entry(),
    write_entry("orders.cancel"),
    read_entry("payments.list"),
]


def _menu(*entries: dict[str, Any]) -> Inventory:
    checked = [_check_entry(entry)[0] for entry in entries]
    return Inventory(capabilities=tuple(c for c in checked if c is not None))


def _step(name: str, **arguments: Any) -> dict[str, Any]:
    return {"capability": name, "arguments": arguments}


def _plan_json(
    *reads: dict[str, Any],
    propose: dict[str, Any] | None = None,
    needs_results: bool = False,
    reason: str = "planned for a test",
) -> str:
    return json.dumps(
        {"reads": list(reads), "propose": propose, "needs_results": needs_results, "reason": reason}
    )


_NOTHING = _plan_json()


def _result(content: str = "", **fields: Any) -> GenerateResult:
    return GenerateResult(
        content=content,
        cost_usd=fields.pop("cost_usd", 0.002),
        finish_reason=fields.pop("finish_reason", "stop"),
        usage=GenerateUsage(prompt_tokens=40, completion_tokens=20),
        **fields,
    )


Scripted = str | GenerateResult | BaseException | Callable[[], Awaitable[GenerateResult]]


class Model:
    """Stands in for ``ModelGatewayClient.generate``: each planning call takes the next
    scripted item (a plan's JSON, a whole result, an exception to raise, or a coroutine
    to await); every other call is the answer."""

    def __init__(self, *plans: Scripted, answer: str = _ANSWER) -> None:
        self._plans = list(plans)
        self._answer = answer
        self.calls: list[dict[str, Any]] = []

    @property
    def planning(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call.get("schema_name") == "plan"]

    async def generate(self, **kwargs: Any) -> GenerateResult:
        self.calls.append(kwargs)
        if kwargs.get("schema_name") != "plan":
            return _result(self._answer, cost_usd=0.001)
        item = self._plans.pop(0) if self._plans else _NOTHING
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, GenerateResult):
            return item
        if callable(item):
            return await item()
        return _result(item)


def _planner_settings(settings: Settings, **planner: Any) -> Settings:
    return settings.model_copy(
        update={"planner": settings.planner.model_validate({"enabled": True, **planner})}
    )


@pytest.fixture
def settings(settings: Settings, request: pytest.FixtureRequest) -> Settings:
    """air-tools-gateway on, ``orders.*`` allowed on the customer channel, the planner
    on. ``@pytest.mark.gateway(...)`` and ``@pytest.mark.planner(...)`` override
    fields; ``@pytest.mark.turn(...)`` overrides ``turn``."""
    gateway_marker = request.node.get_closest_marker("gateway")
    planner_marker = request.node.get_closest_marker("planner")
    turn_marker = request.node.get_closest_marker("turn")
    gateway = settings.downstream.tools_gateway.model_validate(
        {
            "enabled": True,
            "base_url": TOOLS_GATEWAY_BASE_URL,
            "tenant_keys": {_TENANT: {"read": READ_A, "execute": EXECUTE_A}},
            "allow": {"customer": ["orders.*"]},
            **(gateway_marker.kwargs if gateway_marker else {}),
        }
    )
    downstream = settings.downstream.model_copy(update={"tools_gateway": gateway})
    resolved = _planner_settings(
        settings.model_copy(update={"downstream": downstream}),
        **(planner_marker.kwargs if planner_marker else {}),
    )
    if turn_marker:
        turn = resolved.turn.model_validate({**resolved.turn.model_dump(), **turn_marker.kwargs})
        resolved = resolved.model_copy(update={"turn": turn})
    return resolved


def _as(app: FastAPI, *, allow_actions: bool = True, subject: str | None = _SUBJECT) -> None:
    async def principal() -> Principal:
        return Principal(
            key_id="customer",
            name="test",
            channel=Channel.CUSTOMER,
            tenant=_TENANT,
            scopes=frozenset({"chat:write", "session:read"}),
            allow_actions=allow_actions,
            subject=subject,
        )

    app.dependency_overrides[require_principal] = principal


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    built = build_app(settings)
    _as(built)
    return built


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(INVENTORY_URL).mock(
            return_value=httpx.Response(200, json=inventory_body(*_ENTRIES))
        )
        yield router


def planned_app(settings: Settings, *, enabled: bool = True) -> tuple[FastAPI, Model]:
    """For other modules: an app with the gateway and the planner as here, the
    principal a verified customer, and a scripted model that plans nothing."""
    gateway = settings.downstream.tools_gateway.model_validate(
        {
            "enabled": True,
            "base_url": TOOLS_GATEWAY_BASE_URL,
            "tenant_keys": {_TENANT: {"read": READ_A, "execute": EXECUTE_A}},
            "allow": {"customer": ["orders.*"]},
        }
    )
    downstream = settings.downstream.model_copy(update={"tools_gateway": gateway})
    resolved = _planner_settings(settings.model_copy(update={"downstream": downstream}))
    if not enabled:
        resolved = resolved.model_copy(
            update={"planner": resolved.planner.model_copy(update={"enabled": False})}
        )
    app = build_app(resolved)
    _as(app)
    return app, _model(app)


def _model(app: FastAPI, *plans: Scripted, answer: str = _ANSWER) -> Model:
    model = Model(*plans, answer=answer)
    getattr(app.state, STATE_ATTR).model_gateway.generate = model.generate
    return model


def _lookup(mock: respx.MockRouter, name: str, data: Any) -> respx.Route:
    return mock.post(f"{_TOOLS}/{name}").mock(
        return_value=httpx.Response(
            200,
            json={
                "capability": name,
                "data": data,
                "truncated": False,
                "as_of": "2026-11-14T10:02:11Z",
            },
        )
    )


def _proposal_route(mock: respx.MockRouter) -> respx.Route:
    from tests.unit.test_action_execution import _proposal

    return mock.post(f"{_ACTIONS}/orders.cancel/propose").mock(
        return_value=httpx.Response(200, json=_proposal())
    )


async def _chat(
    app: FastAPI,
    message: str = "Where is my order?",
    *,
    stream: bool = False,
    headers: dict[str, str] | None = None,
    **body: Any,
) -> httpx.Response:
    sent = {**auth(CUSTOMER_KEY), **(headers or {})}
    if stream:
        sent["Accept"] = "text/event-stream"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/v1/chat", headers=sent, json={"message": message, **body})
    assert response.status_code == httpx.codes.OK, response.text
    return response


async def _turn(app: FastAPI, message: str = "Where is my order?", **body: Any) -> dict[str, Any]:
    result: dict[str, Any] = (await _chat(app, message, **body)).json()
    return result


def _stage(body: dict[str, Any], stage: Stage) -> dict[str, Any]:
    [record] = [s for s in body["trace"] if s["stage"] == stage]
    return record


def _tool_posts(mock: respx.MockRouter) -> list[str]:
    return [
        c.request.url.path
        for c in mock.calls
        if c.request.method == "POST" and "/v1/tools/" in c.request.url.path
    ]


def _all_posts(mock: respx.MockRouter) -> list[str]:
    return [c.request.url.path for c in mock.calls if c.request.method == "POST"]


# ── REQ-PLN-014 (T1) ─────────────────────────────────────────────────────────


@respx.mock
async def test_the_planner_uses_its_own_model_class(settings: Settings) -> None:
    """``REQ-PLN-014``. The client sends a per-call class over the configured one; the
    planner asks for ``planner.model_class`` while synthesis keeps the configured
    class; and the default is ``cloud``."""
    route = respx.post(f"{MODEL_GATEWAY_BASE_URL}/v1/generate").mock(
        return_value=httpx.Response(200, json={"content": "ok"})
    )
    client = ModelGatewayClient(settings)
    await client.generate(
        messages=[{"role": "user", "content": "hi"}], model_class=ModelClass.LOCAL
    )
    await client.generate(messages=[{"role": "user", "content": "hi"}])
    first, second = (json.loads(call.request.content)["model"] for call in route.calls)
    assert (first, second) == ("local", settings.model_gateway.model_class.value)
    await client.aclose()

    assert Settings().planner.model_class is ModelClass.CLOUD
    app = build_app(_planner_settings(settings, model_class="local"))
    _as(app)
    model = _model(app)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(INVENTORY_URL).mock(
            return_value=httpx.Response(200, json=inventory_body(*_ENTRIES))
        )
        await _turn(app)
    [planning] = model.planning
    [synthesis] = [call for call in model.calls if call is not planning]
    assert planning["model_class"] is ModelClass.LOCAL
    assert synthesis.get("model_class") is None  # the client's configured class


# ── REQ-PLN-001 ──────────────────────────────────────────────────────────────


async def test_a_turn_with_capabilities_is_planned_by_one_structured_call(
    app: FastAPI, settings: Settings, mock: respx.MockRouter
) -> None:
    """``REQ-PLN-001``, AC-1: one planning call with a ``json_schema`` drives one
    lookup. No call with the planner off, with nothing offered, or on a confirmation
    turn."""
    lookup = _lookup(mock, "orders.list_recent", {"orders": [{"order_id": "48213"}]})
    model = _model(app, _plan_json(_step("orders.list_recent", limit=5)))

    body = await _turn(app)

    [planning] = model.planning
    assert planning["json_schema"]["required"] == ["reads", "propose", "needs_results", "reason"]
    assert lookup.call_count == 1
    assert json.loads(lookup.calls.last.request.content)["arguments"] == {"limit": 5}
    assert _stage(body, Stage.PLAN) == {
        "stage": Stage.PLAN,
        "status": "ok",
        "latency_ms": _stage(body, Stage.PLAN)["latency_ms"],
        "detail": "reads=1 propose=no needs_results=no",
    }
    assert body["routes"] == [Route.TOOLS]

    # Planner off: the rule, and no planning call.
    off = build_app(
        settings.model_copy(
            update={"planner": settings.planner.model_copy(update={"enabled": False})}
        )
    )
    _as(off)
    model = _model(off)
    assert _stage(await _turn(off), Stage.PLAN)["detail"] == "planner disabled"
    assert model.planning == []

    # Nothing offered: an anonymous customer, and every capability needs one.
    anonymous = build_app(settings)
    _as(anonymous, subject=None)
    model = _model(anonymous)
    assert _stage(await _turn(anonymous), Stage.PLAN)["detail"] == "no capabilities offered"
    assert model.planning == []


async def test_a_confirmation_turn_is_not_planned(app: FastAPI, mock: respx.MockRouter) -> None:
    """``REQ-PLN-001``, ``011``: a turn confirming a real proposal makes no planning
    call, and ``PLAN`` says why."""
    _proposal_route(mock)
    mock.post(f"{_ACTIONS}/orders.cancel/execute").mock(
        return_value=httpx.Response(
            200, json={"status": "done", "execution_id": "gx_1", "capability": "orders.cancel"}
        )
    )
    model = _model(app, _plan_json(propose=_step("orders.cancel", order_id="55120")))
    proposed = await _turn(app, "Cancel order 55120")
    assert proposed["proposal"] is not None, proposed["trace"]

    confirmed = await _turn(
        app,
        "yes",
        session_id=proposed["session_id"],
        confirm={"proposal_id": proposed["proposal"]["proposal_id"], "approve": True},
    )

    assert len(model.planning) == 1
    assert _stage(confirmed, Stage.PLAN)["status"] == "skipped"
    assert _stage(confirmed, Stage.PLAN)["detail"] == "nothing to plan: a confirmation turn"
    assert _stage(confirmed, Stage.GATHER)["detail"] == "orders.cancel done"


# ── REQ-PLN-002 ──────────────────────────────────────────────────────────────


async def test_the_planner_prompt_holds_no_identity_and_only_offered_capabilities(
    settings: Settings, mock: respx.MockRouter
) -> None:
    """``REQ-PLN-002``, AC-5. Through the real ``air-token`` path: the subject, the
    tenant, the API key, the gateway keys and the token are nowhere in the planning
    call. Only offered capabilities are: not ``payments.list`` (not allowed), not
    ``orders.cancel`` for a key that may not propose. The history is there, masked."""
    security = settings.security.model_copy(update={"customer_tokens": [configured_key()]})
    app = build_app(settings.model_copy(update={"security": security}))
    model = _model(app)
    token = mint()

    first = await _turn(app, "Hi, my email is jane@example.com", headers={"air-token": token})
    await _turn(
        app, "Where is my order?", headers={"air-token": token}, session_id=first["session_id"]
    )

    sent = json.dumps(
        [call["messages"] for call in model.planning] + [c["json_schema"] for c in model.planning]
    )
    for secret in (_SUBJECT, _TENANT, CUSTOMER_KEY, READ_A, EXECUTE_A, *token.split(".")):
        assert secret not in sent
    assert "orders.list_recent" in sent and "orders.get" in sent
    assert "payments.list" not in sent
    prompts = "\n".join(m["content"] for call in model.planning for m in call["messages"])
    assert '<data source="capabilities">' in prompts
    assert '<data source="conversation">' in prompts
    assert "jane@example.com" not in sent
    assert "Where is my order?" in sent

    # A key that may not propose is offered no write, so its plan cannot name one.
    no_writes = build_app(settings)
    _as(no_writes, allow_actions=False)
    model = _model(no_writes)
    await _turn(no_writes)
    [planning] = model.planning
    assert "orders.cancel" not in json.dumps(planning)
    assert planning["json_schema"]["properties"]["propose"] == {"type": "null"}

    messages = planning_messages(
        "rules",
        text="hello",
        menu=_menu(read_entry("orders.list_recent")),
        history=(),
        intent="order_status",
        sentiment="negative",
    )
    assert "intent=order_status, sentiment=negative" in json.dumps(messages)


# ── REQ-PLN-003 ──────────────────────────────────────────────────────────────


_BAD_PLANS = {
    "four reads": _plan_json(*[_step("orders.get", order_id="1234")] * 4),
    "an extra key": json.dumps({**json.loads(_NOTHING), "tenant": "x"}),
    "an extra key in a step": _plan_json({**_step("orders.get"), "subject": "x"}),
    "needs_results a string": json.dumps({**json.loads(_NOTHING), "needs_results": "yes"}),
    "reads not a list": json.dumps({**json.loads(_NOTHING), "reads": "orders.get"}),
    "arguments a list": _plan_json({"capability": "orders.get", "arguments": []}),
    "capability a number": _plan_json({"capability": 7, "arguments": {}}),
    "propose a string": json.dumps({**json.loads(_NOTHING), "propose": "orders.cancel"}),
    "a missing field": json.dumps({"reads": [], "propose": None, "reason": "r"}),
    "an array": "[]",
    "prose": "sure! here's a plan:",
}


@pytest.mark.parametrize("case", list(_BAD_PLANS))
def test_the_plan_is_validated_against_its_schema(case: str) -> None:
    """``REQ-PLN-003``. Each bad output is no plan; a good one parses. The round-1
    schema enumerates exactly the offered names, ``propose`` is ``null``-only with
    no write on offer, and round 2's schema has no ``reads``."""
    assert parse_plan(_BAD_PLANS[case], max_reads=3) is None
    good = parse_plan(
        _plan_json(_step("orders.get", order_id="1234"), needs_results=True), max_reads=3
    )
    assert good is not None and good.needs_results and good.reads[0].capability == "orders.get"
    # A long reason is cut, not a lost plan: it is only logged (Gemini wrote 216).
    long = parse_plan(
        _plan_json(_step("orders.get", order_id="1234"), reason="x" * 216), max_reads=3
    )
    assert long is not None and len(long.reason) == 200

    menu = _menu(read_entry("orders.list_recent"), _get_entry(), write_entry("orders.cancel"))
    schema = plan_schema(menu, max_reads=3)
    reads = schema["properties"]["reads"]
    assert reads["maxItems"] == 3
    assert step_names(reads["items"]) == ["orders.get", "orders.list_recent"]
    assert step_names(schema["properties"]["propose"]) == ["orders.cancel"]
    # Each name carries its own arguments' shape, so a constraining provider can fill it.
    [get] = [
        b
        for b in reads["items"]["anyOf"]
        if b["properties"]["capability"]["enum"] == ["orders.get"]
    ]
    assert get["properties"]["arguments"]["required"] == ["order_id"]
    assert "order_id" in get["properties"]["arguments"]["properties"]
    reads_only = plan_schema(_menu(_get_entry()), max_reads=3)
    assert reads_only["properties"]["propose"] == {"type": "null"}
    second = follow_up_schema(menu)
    assert "reads" not in second["properties"] and second["additionalProperties"] is False
    assert parse_follow_up(_plan_json()) is None  # round 2 refuses `reads`


# ── REQ-PLN-004 ──────────────────────────────────────────────────────────────


async def _hang() -> GenerateResult:
    await asyncio.sleep(30)
    raise AssertionError("not reached")


_FAILURES: dict[str, Scripted] = {
    "prose": "sure! here's a plan:",
    "a refusal": _result("", refusal=True, finish_reason="refusal"),
    "cut off": _result('{"reads": [', finish_reason="length"),
    "a gateway error": ModelGatewayError("gateway returned 503", retryable=True),
}


@pytest.mark.parametrize("case", list(_FAILURES))
async def test_an_invalid_plan_falls_back_and_the_turn_answers(
    app: FastAPI, mock: respx.MockRouter, case: str
) -> None:
    """``REQ-PLN-004``, AC-3. No gateway call; ``PLAN`` degraded naming
    air-model-gateway; the turn answers from the model as it would without tools."""
    model = _model(app, _FAILURES[case])

    body = await _turn(app)

    assert _all_posts(mock) == []
    plan = _stage(body, Stage.PLAN)
    assert plan["status"] == "degraded"
    assert body["degraded"] == ["air-model-gateway"]
    assert body["status"] == "degraded"
    assert body["answer"] == _ANSWER
    assert body["routes"] == [Route.DIRECT]
    assert len(model.planning) == 1


async def test_a_failed_plan_tells_the_answer_nothing_was_looked_up_or_prepared(
    app: FastAPI, mock: respx.MockRouter
) -> None:
    """``REQ-PLN-017`` (OR-015). A planner timeout on "Cancel order 55120" leaves the
    answer prompt saying no lookup was made and no change prepared, so it cannot ask
    for a confirmation that leads nowhere. A turn that planned fine carries neither."""
    from air_orchestrator_service.engine import turn

    model = _model(app, _hang)
    body = await _turn(app, "Cancel order 55120. Also, where is 48213?")

    assert _all_posts(mock) == []
    assert _stage(body, Stage.PLAN)["status"] == "degraded"
    assert body["answer"] == _ANSWER and body["proposal"] is None
    [synthesis] = [call for call in model.calls if call not in model.planning]
    prompt = "\n".join(m["content"] for m in synthesis["messages"] if m["role"] == "system")
    assert turn._LOOKUPS_FAILED.format(names=turn._ACCOUNT_LOOKUPS) in prompt
    assert turn._CHANGE_NOT_PREPARED in prompt

    model = _model(app, _plan_json())
    await _turn(app, "hi")
    [synthesis] = [call for call in model.calls if call not in model.planning]
    prompt = "\n".join(m["content"] for m in synthesis["messages"] if m["role"] == "system")
    assert turn._CHANGE_NOT_PREPARED not in prompt
    assert turn._ACCOUNT_LOOKUPS not in prompt


# ── REQ-PLN-005 ──────────────────────────────────────────────────────────────


@pytest.mark.turn(deadline_ms=2000)
async def test_planning_is_bounded_and_counted_against_the_ceiling(
    app: FastAPI, settings: Settings, mock: respx.MockRouter
) -> None:
    """``REQ-PLN-005``. The call's timeout is at most ``deadline_share`` of the turn;
    a call that outlasts it falls back and the turn answers; its cost is in usage;
    and a ceiling too small to plan in makes no call."""
    _lookup(mock, "orders.list_recent", {"orders": []})
    model = _model(app, _plan_json(_step("orders.list_recent", limit=1)))
    body = await _turn(app)
    [planning] = model.planning
    assert 0 < planning["timeout"] <= 0.3 * 2.0
    assert body["usage"]["planning_cost_usd"] == pytest.approx(0.002)
    assert body["usage"]["cost_usd"] == pytest.approx(0.003)
    assert body["usage"]["model_calls"] == 2

    model = _model(app, _hang)
    body = await _turn(app)
    plan = _stage(body, Stage.PLAN)
    assert (plan["status"], plan["detail"]) == ("degraded", "planner call timed out after 0.60s")
    assert body["answer"] == _ANSWER and body["degraded"] == ["air-model-gateway"]

    tight = build_app(
        settings.model_copy(
            update={"turn": settings.turn.model_copy(update={"max_cost_usd": 0.0001})}
        )
    )
    _as(tight)
    model = _model(tight)
    body = await _turn(tight)
    assert model.planning == []
    plan = _stage(body, Stage.PLAN)
    assert (plan["status"], plan["detail"]) == ("degraded", "cost ceiling: nothing left to plan")
    assert _all_posts(mock) == ["/v1/tools/orders.list_recent"]


# ── REQ-PLN-006 ──────────────────────────────────────────────────────────────


@pytest.mark.gateway(allow={"customer": ["orders.get"]})
async def test_the_planner_cannot_widen_the_offered_capabilities(
    app: FastAPI, mock: respx.MockRouter
) -> None:
    """``REQ-PLN-006``, AC-4. Only ``orders.get`` is offered: the schema says so, and
    a plan naming ``orders.list_recent`` and ``payments.refund`` anyway calls
    neither. Both are named in the stage detail, and the turn answers. A planned
    write this key was never offered is dropped too."""
    model = _model(
        app,
        _plan_json(_step("orders.list_recent", limit=1), _step("payments.refund", amount=10)),
    )

    body = await _turn(app)

    assert step_names(model.planning[0]["json_schema"]["properties"]["reads"]["items"]) == [
        "orders.get"
    ]
    assert _all_posts(mock) == []
    detail = _stage(body, Stage.GATHER)["detail"]
    assert "orders.list_recent dropped: not offered" in detail
    assert "payments.refund dropped: not offered" in detail
    assert body["answer"] == _ANSWER

    _model(app, _plan_json(_step("orders.get", order_id="not-a-number")))
    body = await _turn(app)
    assert _stage(body, Stage.GATHER)["detail"] == "orders.get dropped: invalid arguments"
    assert _all_posts(mock) == []


# ── REQ-PLN-007, 008 (T4) ────────────────────────────────────────────────────


async def test_a_dependent_proposal_takes_exactly_two_rounds(
    app: FastAPI, mock: respx.MockRouter
) -> None:
    """``REQ-PLN-007``, AC-2. Round 1 reads ``orders.list_recent`` and asks for its
    results; round 2 sees them, delimited, and proposes cancelling 55120. Exactly two
    planning calls, one propose request for 55120, and round 2 reported on ``GATHER``."""
    _lookup(mock, "orders.list_recent", {"orders": [{"order_id": "55120", "status": "placed"}]})
    propose = _proposal_route(mock)
    model = _model(
        app,
        _plan_json(_step("orders.list_recent", limit=1), needs_results=True),
        json.dumps({"propose": _step("orders.cancel", order_id="55120"), "reason": "cancel it"}),
    )

    body = await _turn(app, "Cancel my last order")

    first, second = model.planning
    assert "reads" not in second["json_schema"]["properties"]
    round_two = "\n".join(m["content"] for m in second["messages"])
    assert '<data source="tool_result">' in round_two and "55120" in round_two
    assert '<data source="first_plan">' in round_two
    assert "55120" not in json.dumps(first["messages"])
    assert propose.call_count == 1
    assert json.loads(propose.calls.last.request.content)["arguments"] == {"order_id": "55120"}
    gather = _stage(body, Stage.GATHER)["detail"]
    assert gather == "orders.list_recent ok; round 2: propose orders.cancel; orders.cancel proposed"
    assert body["proposal"] is not None and body["proposal"]["action"] == "orders.cancel"
    assert len(model.calls) == 2  # no synthesis: a proposing turn's answer is fixed text

    # Without `needs_results`, no second round.
    _lookup(mock, "orders.list_recent", {"orders": []})
    model = _model(app, _plan_json(_step("orders.list_recent", limit=1)))
    await _turn(app)
    assert len(model.planning) == 1


async def test_round_two_arguments_must_validate(app: FastAPI, mock: respx.MockRouter) -> None:
    """``REQ-PLN-008``. An identifier round 2 copies from a result is only used if it
    validates against the capability's schema: no propose request is sent."""
    _lookup(mock, "orders.list_recent", {"orders": [{"order_id": "55120; DROP"}]})
    propose = _proposal_route(mock)
    _model(
        app,
        _plan_json(_step("orders.list_recent", limit=1), needs_results=True),
        json.dumps({"propose": _step("orders.cancel", order_id="55120; DROP"), "reason": "r"}),
    )

    body = await _turn(app, "Cancel my last order")

    assert propose.call_count == 0
    assert _stage(body, Stage.GATHER)["detail"].endswith(
        "round 2: propose orders.cancel; orders.cancel dropped: invalid arguments"
    )
    assert body["proposal"] is None


# ── REQ-PLN-009 is in test_prompts.py; REQ-PLN-010, 011, 013 (T5) ────────────


async def test_the_propose_trigger_is_inert_when_the_planner_is_enabled(
    app: FastAPI, settings: Settings, mock: respx.MockRouter
) -> None:
    """``REQ-PLN-010``, AC-6. ``/propose`` makes no stand-in proposal with the planner
    on: planned, failed, or with nothing offered. With it off, it still does."""
    for script in (_NOTHING, ModelGatewayError("gateway returned 503", retryable=True)):
        _model(app, script)
        body = await _turn(app, "/propose")
        assert body["proposal"] is None, body["trace"]

    anonymous = build_app(settings)
    _as(anonymous, subject=None)
    _model(anonymous)
    assert (await _turn(anonymous, "/propose"))["proposal"] is None

    off = build_app(
        settings.model_copy(
            update={"planner": settings.planner.model_copy(update={"enabled": False})}
        )
    )
    _as(off)
    _model(off)
    assert (await _turn(off, "/propose"))["proposal"]["action"] == "echo.noop"


async def test_the_plan_stage_reports_what_ran(
    app: FastAPI, settings: Settings, mock: respx.MockRouter
) -> None:
    """``REQ-PLN-011``. One ``PLAN`` event, in ``Stage`` order, saying what ran; round
    2 on ``GATHER`` — a change, no change, or its failure, which keeps the reads,
    proposes nothing and tells the answer the change was not prepared."""
    _lookup(mock, "orders.list_recent", {"orders": [{"order_id": "55120"}]})
    model = _model(
        app,
        _plan_json(_step("orders.list_recent", limit=1), needs_results=True),
        json.dumps({"propose": None, "reason": "nothing to cancel"}),
    )
    response = await _chat(app, "Cancel my last order", stream=True)
    events = [data for name, data in parse_sse(response.text) if name == "stage"]
    stages = [data["stage"] for data in events]
    assert stages.count(Stage.PLAN) == 1
    assert stages.index(Stage.CLASSIFY) < stages.index(Stage.PLAN) < stages.index(Stage.GATHER)
    by_stage = {data["stage"]: data for data in events}
    assert by_stage[Stage.PLAN]["detail"] == "reads=1 propose=no needs_results=yes"
    assert by_stage[Stage.GATHER]["detail"] == "orders.list_recent ok; round 2: no change"

    model = _model(
        app,
        _plan_json(_step("orders.list_recent", limit=1), needs_results=True),
        ModelGatewayError("gateway returned 503", retryable=True),
    )
    body = await _turn(app, "Cancel my last order")
    gather_record = _stage(body, Stage.GATHER)
    assert gather_record["status"] == "degraded"
    assert gather_record["detail"] == (
        "orders.list_recent ok; round 2: planner call failed: gateway returned 503"
    )
    assert body["proposal"] is None and body["status"] == "degraded"
    assert body["degraded"] == ["air-model-gateway"]
    [answer_call] = [c for c in model.calls if c.get("schema_name") != "plan"]
    prompt = json.dumps(answer_call["messages"])
    assert "55120" in prompt and "could not prepare it" in prompt
    assert body["answer"] == _ANSWER


async def test_an_unavailable_inventory_is_named_in_the_plan_stage(
    app: FastAPI, mock: respx.MockRouter
) -> None:
    """``REQ-PLN-011``, OR-011. With the gateway down nothing is offered, so nothing is
    planned; ``PLAN`` says the inventory was unavailable and names air-tools-gateway,
    rather than reading like a tenant with nothing on offer. No planning call is made."""
    mock.get(INVENTORY_URL).mock(return_value=httpx.Response(503))
    model = _model(app)

    detail = "no capabilities offered: air-tools-gateway inventory unavailable"
    body = await _turn(app, "Where is my order 48213?")
    response = await _chat(app, "Where is my order 48213?", stream=True)

    plan = _stage(body, Stage.PLAN)
    assert (plan["status"], plan["detail"]) == ("skipped", detail)
    [event] = [
        data
        for name, data in parse_sse(response.text)
        if name == "stage" and data["stage"] == Stage.PLAN
    ]
    assert (event["detail"], event["downstream"]) == (detail, "air-tools-gateway")
    assert model.planning == []
    assert body["answer"] == _ANSWER


async def test_a_plan_with_lookups_also_retrieves(
    settings: Settings, mock: respx.MockRouter
) -> None:
    """``REQ-PLN-015``. With air-rag-service on, a model plan with a read still
    searches the knowledge base, so a message asking about an order and a policy
    gets both. A plan with no capability step retrieves as spec 0011 decides."""
    from tests.unit.test_gather_stage import RAG_BASE_URL

    rag = settings.downstream.rag.model_copy(update={"enabled": True, "base_url": RAG_BASE_URL})
    app = build_app(
        settings.model_copy(
            update={"downstream": settings.downstream.model_copy(update={"rag": rag})}
        )
    )
    _as(app)
    retrieve = mock.post(f"{RAG_BASE_URL}/v1/retrieve").mock(
        return_value=httpx.Response(200, json={"query": "q", "citations": []})
    )
    _lookup(mock, "orders.get", {"order_id": "48213", "status": "in_transit"})
    _model(app, _plan_json(_step("orders.get", order_id="48213")), _plan_json())

    looked_up = await _turn(app, "Where is order 48213, and how long do refunds take?")
    assert retrieve.call_count == 1
    assert looked_up["routes"] == ["rag", "tools"]
    assert _stage(looked_up, Stage.GATHER)["detail"] == "no relevant sources; orders.get ok"

    policy = await _turn(app, "How long do refunds take?")
    assert retrieve.call_count == 2
    assert "rag" in policy["routes"]


async def test_a_knowledge_base_failure_does_not_degrade_a_turn_with_lookups(
    settings: Settings, mock: respx.MockRouter
) -> None:
    """``REQ-PLN-015``. Beside lookups the knowledge base is an extra: when it fails
    the turn is still ``ok``, nothing is listed as degraded, and the stage detail
    says it was not consulted."""
    from tests.unit.test_gather_stage import RAG_BASE_URL

    rag = settings.downstream.rag.model_copy(update={"enabled": True, "base_url": RAG_BASE_URL})
    app = build_app(
        settings.model_copy(
            update={"downstream": settings.downstream.model_copy(update={"rag": rag})}
        )
    )
    _as(app)
    mock.post(f"{RAG_BASE_URL}/v1/retrieve").mock(return_value=httpx.Response(503))
    _lookup(mock, "orders.get", {"order_id": "48213", "status": "in_transit"})
    _model(app, _plan_json(_step("orders.get", order_id="48213")))

    body = await _turn(app, "Where is order 48213?")

    assert body["status"] == "ok"
    assert body["degraded"] == []
    gather = _stage(body, Stage.GATHER)
    assert gather["status"] == "ok"
    assert gather["detail"].startswith("orders.get ok; knowledge base not consulted: ")


def _planner_count(round_no: int, outcome: str) -> float:
    value = metrics.REGISTRY.get_sample_value(
        "air_orchestrator_service_planner_calls_total", {"round": str(round_no), "outcome": outcome}
    )
    return value or 0.0


@pytest.mark.turn(deadline_ms=2000)
async def test_every_planning_call_is_counted(
    app: FastAPI, settings: Settings, mock: respx.MockRouter
) -> None:
    """``REQ-PLN-012``. One count per planning call, by round and outcome."""
    keys = [(1, o) for o in ("ok", "invalid", "timeout", "error", "skipped_budget")] + [(2, "ok")]
    before = {key: _planner_count(*key) for key in keys}
    _lookup(mock, "orders.list_recent", {"orders": [{"order_id": "55120"}]})

    _model(
        app,
        _plan_json(_step("orders.list_recent", limit=1), needs_results=True),
        json.dumps({"propose": None, "reason": "r"}),
    )
    await _turn(app)
    _model(app, "not json")
    await _turn(app)
    _model(app, _hang)
    await _turn(app)
    _model(app, ModelGatewayError("gateway returned 503", retryable=True))
    await _turn(app)
    tight = build_app(
        settings.model_copy(
            update={"turn": settings.turn.model_copy(update={"max_cost_usd": 0.0001})}
        )
    )
    _as(tight)
    _model(tight)
    await _turn(tight)

    assert {key: _planner_count(*key) - before[key] for key in keys} == dict.fromkeys(keys, 1.0)


async def test_the_plan_never_reaches_the_client(app: FastAPI, mock: respx.MockRouter) -> None:
    """``REQ-PLN-013``. Neither round's ``reason``, nor the catalogue, is in any event
    or in the JSON result. The ``route`` reason is fixed text."""
    _lookup(mock, "orders.list_recent", {"orders": [{"order_id": "55120"}]})
    reasons = ("ROUND-ONE-REASON-7f3a", "ROUND-TWO-REASON-9c1d")
    scripts = (
        _plan_json(_step("orders.list_recent", limit=1), needs_results=True, reason=reasons[0]),
        json.dumps({"propose": None, "reason": reasons[1]}),
    )

    _model(app, *scripts)
    streamed = (await _chat(app, stream=True)).text
    _model(app, *scripts)
    body = await _chat(app)

    for text in (streamed, body.text):
        for secret in (*reasons, _CATALOGUE_MARKER, '"needs_results"', "input_schema"):
            assert secret not in text
    route = next(data for name, data in parse_sse(streamed) if name == "route")
    assert route["reason"] == "planned: 1 lookup"
    assert route["capabilities"] == ["orders.list_recent"]


# ── A unit check of the fallback, with no app ────────────────────────────────


async def test_the_fallback_keeps_no_stand_in(settings: Settings) -> None:
    """``REQ-PLN-010`` at the planner: with nothing offered, the plan is the rule's
    without the ``/propose`` stand-in, and no call is made."""
    gateway = ModelGatewayClient(settings)
    planner = ModelPlanner(_planner_settings(settings), gateway, PromptRegistry(settings))
    principal = Principal(
        key_id="k", name="k", channel=Channel.CUSTOMER, tenant=_TENANT, allow_actions=True
    )

    outcome = await planner.plan(
        PlanContext(text="/propose", principal=principal, confirming=False, rag_serves=False)
    )

    assert outcome.plan.stand_in_proposal is False
    assert (outcome.detail, outcome.usage) == ("no capabilities offered", None)
    await gateway.aclose()
