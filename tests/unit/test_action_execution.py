"""Proposing and executing an air-tools-gateway ``write`` capability on real turns —
spec 0012 T8 and T9.

The full app, the gateway a respx double, the plan injected with ``FixedPlanner`` and
the principal injected with a verified subject (spec 0014 has not landed). A proposing
turn sends no ``/execute`` request; only a later turn carrying the structured
``confirm``, from the same key and customer, does, once.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from air_orchestrator_service.api.deps import STATE_ATTR, require_principal
from air_orchestrator_service.clients.base import BreakerState
from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import Channel, Route, Stage
from air_orchestrator_service.engine.plan import Plan, Step
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.schemas.common import Principal
from air_orchestrator_service.schemas.session import Session
from tests.conftest import (
    BUSINESS_KEY,
    CUSTOMER_KEY,
    TOOLS_GATEWAY_BASE_URL,
    FixedPlanner,
    auth,
    build_app,
    use_planner,
)
from tests.unit._tools_gateway_support import (
    EXECUTE_A,
    INVENTORY_URL,
    READ_A,
    inventory_body,
    read_entry,
    write_entry,
)
from tests.unit.test_events import parse_sse
from tests.unit.test_gather_stage import _gateway

_ACTIONS = f"{TOOLS_GATEWAY_BASE_URL}/v1/actions"
_PROPOSE = f"{_ACTIONS}/orders.cancel/propose"
_EXECUTE = f"{_ACTIONS}/orders.cancel/execute"
_SUBJECT = "cust_123"
_GATEWAY_ID = "gp_7c1e4a90b2d3"
_SUMMARY = "Cancel order 55120 (₹1,240.00). The refund goes to your original payment method."
_CANCEL = Step("orders.cancel", {"order_id": "55120"})
_DIRECT = Plan(routes=(Route.DIRECT,), reason="direct answer")


def _proposal(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "proposal_id": _GATEWAY_ID,
        "capability": "orders.cancel",
        "risk": "low",
        "summary": _SUMMARY,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }
    body.update(overrides)
    return body


def _problem(code: str, status: int = 422) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "type": f"https://air.local/problems/{code}",
            "title": "No",
            "status": status,
            "code": code,
        },
        headers={"Content-Type": "application/problem+json"},
    )


@pytest.fixture
def settings(settings: Settings) -> Settings:
    gateway = settings.downstream.tools_gateway.model_validate(
        {
            "enabled": True,
            "base_url": TOOLS_GATEWAY_BASE_URL,
            "tenant_keys": {"tenant-a": {"read": READ_A, "execute": EXECUTE_A}},
            "allow": {"customer": ["orders.*"], "business": ["orders.*"]},
        }
    )
    downstream = settings.downstream.model_copy(update={"tools_gateway": gateway})
    return settings.model_copy(update={"downstream": downstream})


def _as(
    app: FastAPI,
    *,
    channel: Channel = Channel.CUSTOMER,
    allow_actions: bool = True,
    key_id: str | None = None,
    subject: str | None = _SUBJECT,
) -> None:
    async def principal() -> Principal:
        return Principal(
            key_id=key_id or ("customer" if channel is Channel.CUSTOMER else "business"),
            name="test",
            channel=channel,
            tenant="tenant-a",
            scopes=frozenset({"chat:write", "query:write", "session:read"}),
            allow_actions=allow_actions,
            subject=subject if channel is Channel.CUSTOMER else None,
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
            return_value=httpx.Response(
                200,
                json=inventory_body(
                    read_entry("orders.list_recent"),
                    write_entry("orders.cancel"),
                    read_entry("orders.catalogue", requires_subject=False),
                ),
            )
        )
        yield router


def _plan(write: Step = _CANCEL) -> FixedPlanner:
    return FixedPlanner(Plan(routes=(Route.ACTION,), reason="injected", write=write))


async def _turn(
    app: FastAPI,
    planner: FixedPlanner | None = None,
    *,
    path: str = "/v1/chat",
    stream: bool = False,
    key: str = CUSTOMER_KEY,
) -> httpx.Response:
    use_planner(app, planner or _plan())
    field = "message" if path == "/v1/chat" else "query"
    headers = {**auth(key), **({"Accept": "text/event-stream"} if stream else {})}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(path, headers=headers, json={field: "Cancel order 55120"})
    assert response.status_code == httpx.codes.OK, response.text
    return response


def _gather(body: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in body["trace"] if s["stage"] == Stage.GATHER)


def _posts(mock: respx.MockRouter) -> list[str]:
    return [c.request.url.path for c in mock.calls if c.request.method == "POST"]


async def test_a_proposing_turn_sends_no_execute_request(
    app: FastAPI, mock: respx.MockRouter
) -> None:
    """``REQ-TGW-012``, AC-4. One propose request, carrying the principal's subject; a
    ``prop_`` proposal; no execute; and no model call, since the answer is fixed text
    built from the gateway's summary."""
    calls = _gateway(app)
    propose = mock.post(_PROPOSE).mock(return_value=httpx.Response(200, json=_proposal()))
    execute = mock.post(_EXECUTE).mock(return_value=httpx.Response(200, json={}))

    body = (await _turn(app)).json()

    assert _posts(mock) == ["/v1/actions/orders.cancel/propose"]
    assert propose.call_count == 1 and execute.call_count == 0
    assert json.loads(propose.calls.last.request.content) == {
        "arguments": {"order_id": "55120"},
        "subject": _SUBJECT,
    }
    assert body["proposal"]["proposal_id"].startswith("prop_")
    assert body["proposal"]["action"] == "orders.cancel"
    assert body["routes"] == ["action"]
    assert body["answer"].startswith(_SUMMARY)
    assert "Nothing has been changed yet" in body["answer"]
    assert _gather(body)["detail"] == "orders.cancel proposed"
    assert calls == []


async def test_the_gateway_proposal_id_never_reaches_the_caller(
    app: FastAPI, mock: respx.MockRouter
) -> None:
    """``REQ-TGW-013``. The gateway's id is on the pending proposal and nowhere a
    caller can read: not the JSON body, not the stream, not the session view, not the
    pending proposal's ``repr``."""
    _gateway(app)
    mock.post(_PROPOSE).mock(return_value=httpx.Response(200, json=_proposal()))

    as_json = await _turn(app)
    as_stream = await _turn(app, stream=True)
    session_id = as_json.json()["session_id"]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        view = await http.get(f"/v1/sessions/{session_id}", headers=auth(CUSTOMER_KEY))

    assert view.status_code == httpx.codes.OK, view.text
    for text in (as_json.text, as_stream.text, view.text):
        assert _GATEWAY_ID not in text
    [event] = [data for name, data in parse_sse(as_stream.text) if name == "proposal"]
    assert event["proposal"]["proposal_id"].startswith("prop_")

    state = getattr(app.state, STATE_ATTR)
    # Read as its owner: a session belongs to the customer who started it (REQ-CTK-007).
    owner = Principal(
        key_id="customer",
        name="test",
        channel=Channel.CUSTOMER,
        tenant="tenant-a",
        subject=_SUBJECT,
    )
    session = await state.sessions.get(session_id, owner)
    assert session is not None
    pending = session.pending
    assert pending is not None
    assert pending.gateway_proposal_id == _GATEWAY_ID
    assert (pending.key_id, pending.subject) == ("customer", _SUBJECT)
    assert _GATEWAY_ID not in repr(pending) and _SUBJECT not in repr(pending)


async def test_the_proposal_takes_the_earlier_expiry_and_a_known_risk(
    app: FastAPI, settings: Settings, mock: respx.MockRouter
) -> None:
    """``REQ-TGW-014``. ``risk`` and ``summary`` are the gateway's; the expiry is the
    earlier of the gateway's and the session's proposal TTL; a risk outside the
    closed set is no proposal at all."""
    _gateway(app)
    soon = datetime.now(UTC) + timedelta(seconds=30)
    later = datetime.now(UTC) + timedelta(hours=2)
    mock.post(_PROPOSE).mock(
        side_effect=[
            httpx.Response(200, json=_proposal(risk="medium", expires_at=soon.isoformat())),
            httpx.Response(200, json=_proposal(expires_at=later.isoformat())),
            httpx.Response(200, json=_proposal(risk="extreme")),
        ]
    )
    ttl = timedelta(seconds=settings.session.proposal_ttl_seconds)

    gateway_first = (await _turn(app)).json()
    ours_first = (await _turn(app)).json()
    unknown_risk = (await _turn(app)).json()

    assert gateway_first["proposal"]["risk"] == "medium"
    assert gateway_first["proposal"]["summary"] == _SUMMARY
    assert datetime.fromisoformat(gateway_first["proposal"]["expires_at"]) == soon
    ours = datetime.fromisoformat(ours_first["proposal"]["expires_at"])
    assert ours < later and abs(ours - (datetime.now(UTC) + ttl)) < timedelta(seconds=30)
    assert unknown_risk["proposal"] is None
    assert unknown_risk["status"] == "degraded"
    assert unknown_risk["answer"].startswith("I couldn't prepare that change just now")


async def test_a_key_without_allow_actions_sends_no_propose_request(
    app: FastAPI, mock: respx.MockRouter
) -> None:
    """``REQ-TGW-020`` (and spec 0001 ``REQ-MUT-012``). The write is dropped before the
    route event, silently: the turn reads as a direct one."""
    propose = mock.post(_PROPOSE).mock(return_value=httpx.Response(200, json=_proposal()))
    _as(app, allow_actions=False)

    response = await _turn(app, stream=True)

    assert propose.call_count == 0
    events = parse_sse(response.text)
    [route] = [data for name, data in events if name == "route"]
    assert (route["routes"], route["reason"], route["capabilities"]) == (
        ["direct"],
        "direct answer",
        [],
    )
    assert not any(name == "proposal" for name, _ in events)
    # Answered by synthesis, like any direct turn, rather than by a proposal's text.
    [synthesise] = [d for n, d in events if n == "stage" and d["stage"] == "synthesise"]
    assert synthesise["detail"].startswith("prompt=customer_care:")


async def test_the_business_channel_is_never_offered_a_write(
    app: FastAPI, mock: respx.MockRouter
) -> None:
    """``REQ-TGW-026``. The business allow-list admits ``orders.*`` and the key may
    propose, yet no write is offered or proposed on ``/v1/query``."""
    _gateway(app)
    propose = mock.post(_PROPOSE).mock(return_value=httpx.Response(200, json=_proposal()))
    _as(app, channel=Channel.BUSINESS)
    planner = _plan()

    body = (await _turn(app, planner, path="/v1/query", key=BUSINESS_KEY)).json()

    assert propose.call_count == 0
    assert body["proposal"] is None
    [seen] = planner.seen
    assert [c.name for c in seen.offered.capabilities] == ["orders.catalogue"]
    assert all(c.kind == "read" for c in seen.offered.capabilities)


def _refused(outcome: str) -> float:
    value = metrics.REGISTRY.get_sample_value(
        "air_orchestrator_service_downstream_calls_total",
        {"service": "air-tools-gateway", "outcome": outcome},
    )
    return value or 0.0


async def test_a_refused_proposal_is_an_answer_not_a_failure(
    app: FastAPI, settings: Settings, mock: respx.MockRouter
) -> None:
    """``REQ-TGW-028``. Six refusals in a row, past the breaker threshold, leave the
    breaker closed and the turns ``ok``, each answered by its code's fixed text. A
    code outside the closed set is a failure like any other."""
    _gateway(app)
    codes = ["invalid_state", "not_found", "not_permitted", "limit_exceeded", "invalid_state"]
    codes.append("invalid_state")
    assert len(codes) > settings.downstream.tools_gateway.breaker_threshold
    mock.post(_PROPOSE).mock(
        side_effect=[_problem(code, 404 if code == "not_found" else 422) for code in codes]
        + [_problem("something_else")]
    )
    before = _refused("refused")

    answers = [(await _turn(app)).json() for _ in codes]
    other = (await _turn(app)).json()

    state = getattr(app.state, STATE_ATTR)
    assert state.tools_gateway.breaker.state is BreakerState.CLOSED
    assert _refused("refused") == before + len(codes)
    for code, body in zip(codes, answers, strict=True):
        assert body["status"] == "ok" and body["proposal"] is None
        assert _gather(body)["detail"] == f"orders.cancel refused: {code}"
    assert "already" not in answers[1]["answer"]
    assert answers[0]["answer"].startswith("That change isn't possible")
    assert answers[1]["answer"].startswith("I couldn't find that on your account")
    assert other["status"] == "degraded"
    assert _gather(other)["detail"] == "orders.cancel failed"


# ── T9: executing a confirmed proposal ────────────────────────────────────────


def _executed(status: str = "done", **extra: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={"status": status, "execution_id": "gx_93ab10f2", "capability": "orders.cancel"}
        | extra,
    )


async def _proposed(app: FastAPI, mock: respx.MockRouter) -> tuple[str, str]:
    """Propose ``orders.cancel`` on a fresh session: its ``(session_id, prop_ id)``."""
    mock.post(_PROPOSE).mock(return_value=httpx.Response(200, json=_proposal()))
    body = (await _turn(app)).json()
    assert body["proposal"] is not None, body
    return body["session_id"], body["proposal"]["proposal_id"]


async def _confirm_response(app: FastAPI, session_id: str, proposal_id: str) -> httpx.Response:
    """Confirm ``proposal_id`` on ``session_id``, planning a direct answer, so that a
    confirmation that confirms nothing is answered like any other turn."""
    use_planner(app, FixedPlanner(_DIRECT))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            "/v1/chat",
            headers=auth(CUSTOMER_KEY),
            json={
                "message": "yes, go ahead",
                "session_id": session_id,
                "confirm": {"proposal_id": proposal_id, "approve": True},
            },
        )
    return response


async def _confirm(app: FastAPI, session_id: str, proposal_id: str) -> dict[str, Any]:
    response = await _confirm_response(app, session_id, proposal_id)
    assert response.status_code == httpx.codes.OK, response.text
    body: dict[str, Any] = response.json()
    return body


def _open(breaker: Any, settings: Settings) -> None:
    for _ in range(settings.downstream.tools_gateway.breaker_threshold):
        breaker.failed()
    assert breaker.state is BreakerState.OPEN


async def test_the_idempotency_key_is_derived_from_the_proposal_id(
    app: FastAPI, settings: Settings, mock: respx.MockRouter
) -> None:
    """``REQ-TGW-015``, AC-5. One execute request, on the execute key, with
    ``Idempotency-Key: airo-<prop_id>`` and the gateway's id in the body. The lookups'
    breaker being open does not refuse it: the executor has its own."""
    _gateway(app)
    execute = mock.post(_EXECUTE).mock(return_value=_executed())
    session_id, proposal_id = await _proposed(app, mock)
    state = getattr(app.state, STATE_ATTR)
    _open(state.tools_gateway.breaker, settings)

    body = await _confirm(app, session_id, proposal_id)

    assert execute.call_count == 1
    request = execute.calls.last.request
    assert request.headers["Idempotency-Key"] == f"airo-{proposal_id}"
    assert request.headers["X-API-Key"] == EXECUTE_A
    assert json.loads(request.content) == {"proposal_id": _GATEWAY_ID, "subject": _SUBJECT}
    assert body["status"] == "ok"
    assert body["answer"].startswith("Done")
    assert _gather(body)["detail"] == "orders.cancel done"


_OUTCOMES = {
    "done": (_executed(), "Done", "ok", "orders.cancel done"),
    "pending_approval": (
        _executed("pending_approval"),
        "That change needs an approval first",
        "ok",
        "orders.cancel pending_approval",
    ),
    "rejected": (
        _executed("rejected", code="invalid_state"),
        "That change could not be made in the order's current state",
        "ok",
        "orders.cancel rejected: invalid_state",
    ),
    "rejected_other": (
        _executed("rejected", code="upstream_error"),
        "That change could not be made, so nothing",
        "ok",
        "orders.cancel rejected: other",
    ),
    "504": (
        httpx.Response(504),
        "I couldn't confirm whether",
        "degraded",
        "orders.cancel failed",
    ),
    "503": (
        httpx.Response(503),
        "I couldn't confirm whether",
        "degraded",
        "orders.cancel failed",
    ),
    "timeout": (
        httpx.ReadTimeout("slow"),
        "I couldn't confirm whether",
        "degraded",
        "orders.cancel timed out",
    ),
    "unreachable": (
        httpx.ConnectError("down"),
        "I couldn't confirm whether",
        "degraded",
        "orders.cancel unreachable",
    ),
    # The gateway answering that it acted on nothing: not done, and ok.
    "404_not_found": (
        _problem("not_found", 404),
        "That change wasn't made",
        "ok",
        "orders.cancel not done: not_found",
    ),
    "409_proposal_expired": (
        _problem("proposal_expired", 409),
        "That change wasn't made",
        "ok",
        "orders.cancel not done: proposal_expired",
    ),
    # It may have been made under another key: unknown, as any other failure.
    "409_already_executed": (
        _problem("already_executed", 409),
        "I couldn't confirm whether",
        "degraded",
        "orders.cancel failed",
    ),
    "breaker_open": (
        None,
        "I couldn't make that change just now",
        "degraded",
        "orders.cancel circuit open",
    ),
}


@pytest.mark.parametrize("case", list(_OUTCOMES))
async def test_the_answer_follows_the_execute_outcome(
    app: FastAPI, settings: Settings, mock: respx.MockRouter, case: str
) -> None:
    """``REQ-TGW-017``, AC-8. Each outcome gives its own fixed text; only ``done``'s
    says it was done, and none says "cancelled". A request that went out and came
    back with nothing is *unknown*; an open circuit, which sends nothing, is *not
    done*. Both of those are ``degraded`` naming the gateway. An execute answered
    ``not_found`` or ``proposal_expired`` acted on nothing: *not done*, ``ok``, and not
    a breaker failure; ``already_executed`` stays *unknown*."""
    calls = _gateway(app)
    response, starts, status, detail = _OUTCOMES[case]
    execute = mock.post(_EXECUTE)
    if isinstance(response, Exception):
        execute.mock(side_effect=response)
    else:
        execute.mock(return_value=response)
    session_id, proposal_id = await _proposed(app, mock)
    breaker = getattr(app.state, STATE_ATTR).action_executor.breaker
    threshold = settings.downstream.tools_gateway.breaker_threshold
    if response is None:
        _open(breaker, settings)
    else:
        for _ in range(threshold - 1):  # one failure short of opening
            breaker.failed()

    body = await _confirm(app, session_id, proposal_id)

    assert execute.call_count == (0 if response is None else 1)
    assert body["answer"].startswith(starts), body["answer"]
    assert body["status"] == status
    gather = _gather(body)
    assert gather["detail"] == detail
    assert body["degraded"] == (["air-tools-gateway"] if status == "degraded" else [])
    assert "cancelled" not in body["answer"].lower()
    assert body["answer"].startswith("Done") is (case == "done")
    assert calls == []  # fixed text: no model call
    if response is not None:
        # A refusal is the gateway answering: it resets the count. A failure opens it.
        assert breaker.is_open is (status == "degraded"), breaker.state


async def test_an_execute_failure_is_never_retried(app: FastAPI, mock: respx.MockRouter) -> None:
    """``REQ-TGW-018``, AC-5. The execute times out; the same confirmation sent again
    sends nothing, because the proposal was consumed the first time
    (``REQ-MUT-006``)."""
    _gateway(app)
    execute = mock.post(_EXECUTE).mock(return_value=httpx.Response(504))
    session_id, proposal_id = await _proposed(app, mock)

    first = await _confirm(app, session_id, proposal_id)
    again = await _confirm(app, session_id, proposal_id)

    assert execute.call_count == 1
    assert first["answer"].startswith("I couldn't confirm whether")
    assert again["answer"] == "An answer."
    assert again["status"] == "ok"


def _shape(body: dict[str, Any]) -> tuple[Any, ...]:
    stages = [(s["stage"], s["status"], s.get("detail")) for s in body["trace"]]
    return body["answer"], body["status"], body["routes"], body["proposal"], stages


async def test_a_confirmation_from_another_key_or_subject_executes_nothing(
    app: FastAPI, mock: respx.MockRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``REQ-TGW-019``, AC-7. Another key, or another customer, on the same tenant and
    session: nothing executes, and the answer is the one an unknown id gets. The
    proposal is consumed, so its owner cannot confirm it afterwards either.

    The session store already refuses another key's session, with the same 404 for a
    real proposal id as for an unknown one. The engine's own check is what holds when
    a store does not, so ownership is then widened to the tenant to reach it."""
    _gateway(app)
    execute = mock.post(_EXECUTE).mock(return_value=_executed())

    session_id, proposal_id = await _proposed(app, mock)
    _as(app, key_id="k_other")
    real = await _confirm_response(app, session_id, proposal_id)
    made_up = await _confirm_response(app, session_id, "prop_000000000000")
    assert real.status_code == made_up.status_code == httpx.codes.NOT_FOUND
    assert real.json()["detail"] == made_up.json()["detail"]

    monkeypatch.setattr(Session, "owned_by", lambda self, who: self.tenant == who.tenant)
    _as(app)
    session_id, proposal_id = await _proposed(app, mock)
    unknown = await _confirm(app, session_id, "prop_000000000000")
    session_id, proposal_id = await _proposed(app, mock)
    _as(app, key_id="k_other")
    other_key = await _confirm(app, session_id, proposal_id)
    _as(app)
    session_id, proposal_id = await _proposed(app, mock)
    _as(app, subject="cust_456")
    other_subject = await _confirm(app, session_id, proposal_id)
    _as(app)
    owner_after = await _confirm(app, session_id, proposal_id)

    assert execute.call_count == 0
    assert _shape(other_key) == _shape(unknown)
    assert _shape(other_subject) == _shape(unknown)
    assert _shape(owner_after) == _shape(unknown)


async def test_an_execution_writes_one_audit_line_without_arguments(
    app: FastAPI, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    """``REQ-TGW-021``. One ``action.executed`` line, naming this service's proposal
    id, the capability, the outcome, the key and the tenant. No argument value, and not
    the gateway's id (``REQ-TGW-013``)."""
    _gateway(app)
    mock.post(_EXECUTE).mock(return_value=_executed())
    session_id, proposal_id = await _proposed(app, mock)

    with caplog.at_level(logging.INFO):
        await _confirm(app, session_id, proposal_id)

    [line] = [r.getMessage() for r in caplog.records if "action.executed" in r.getMessage()]
    for field in (
        f"'proposal_id': '{proposal_id}'",
        "'capability': 'orders.cancel'",
        "'outcome': 'done'",
        "'key_id': 'customer'",
        "'tenant': 'tenant-a'",
    ):
        assert field in line, line
    assert "55120" not in line and "arguments" not in line
    assert _GATEWAY_ID not in caplog.text


async def test_a_change_not_proposed_still_answers_the_rest(
    app: FastAPI, mock: respx.MockRouter
) -> None:
    """``REQ-TGW-033``, OR-016b. "Cancel 55120, and what are my recent orders?" with
    the cancel refused: the lookup is sent, the model answers the rest told what the
    customer was shown, and the answer is the refusal's text then the model's, as the
    first delta when streamed. A failed synthesis leaves the refusal's text alone."""
    from air_orchestrator_service.clients.model_gateway import ModelGatewayError
    from air_orchestrator_service.engine import turn

    _gateway(app)
    mock.post(_PROPOSE).mock(return_value=_problem("invalid_state"))
    lookup = mock.post(f"{TOOLS_GATEWAY_BASE_URL}/v1/tools/orders.list_recent").mock(
        return_value=httpx.Response(
            200,
            json={
                "capability": "orders.list_recent",
                "data": {"orders": [{"order_id": "48213", "status": "in_transit"}]},
                "truncated": False,
                "as_of": "2026-11-15T10:02:11Z",
            },
        )
    )
    both = FixedPlanner(
        Plan(
            routes=(Route.TOOLS, Route.ACTION),
            reason="injected",
            reads=(Step("orders.list_recent"),),
            write=_CANCEL,
        )
    )
    state = getattr(app.state, STATE_ATTR)
    canned = state.model_gateway.generate
    prompts: list[str] = []
    replies: list[str] = []

    async def generate(**kwargs: Any) -> Any:
        prompts.append("\n".join(m["content"] for m in kwargs["messages"]))
        result = await canned(**kwargs)
        replies.append(result.content)
        return result

    state.model_gateway.generate = generate
    refusal = turn._PROPOSE_REFUSED["invalid_state"]

    body = (await _turn(app, both)).json()

    assert body["status"] == "ok" and body["proposal"] is None
    assert lookup.call_count == 1
    assert body["answer"] == f"{refusal}\n\n{replies[0]}" and replies[0]
    assert "for example once it has shipped" not in refusal
    [prompt] = prompts
    assert turn._CHANGE_ALREADY_ANSWERED in prompt and refusal not in prompt
    assert "48213" in prompt
    assert "rest of your message once" not in body["answer"]  # nothing awaits a confirm

    events = parse_sse((await _turn(app, both, stream=True)).text)
    deltas = [data["text"] for name, data in events if name == "answer.delta"]
    [answer] = [data for name, data in events if name == "answer"]
    assert deltas[0] == f"{refusal}\n\n" and "".join(deltas) == answer["text"]

    async def broken(**kwargs: Any) -> Any:
        raise ModelGatewayError("gateway returned 503", retryable=True)

    state.model_gateway.generate = broken
    failed = (await _turn(app, both)).json()
    assert failed["answer"] == refusal and failed["status"] == "degraded"


async def test_a_proposing_turn_says_the_rest_will_be_answered(
    app: FastAPI, mock: respx.MockRouter
) -> None:
    """``REQ-TGW-032``, OR-010. "Cancel 55120, and where is 48213?": the proposal
    takes the turn, the lookup is not sent, and the answer says the rest will be
    answered after the change is confirmed or declined. Without reads, nothing more."""
    _gateway(app)
    mock.post(_PROPOSE).mock(return_value=httpx.Response(200, json=_proposal()))
    lookup = mock.post(url__startswith=f"{TOOLS_GATEWAY_BASE_URL}/v1/tools/")
    both = FixedPlanner(
        Plan(
            routes=(Route.TOOLS, Route.ACTION),
            reason="injected",
            reads=(Step("orders.get", {"order_id": "48213"}),),
            write=_CANCEL,
        )
    )

    mixed = (await _turn(app, both)).json()
    alone = (await _turn(app)).json()

    assert mixed["proposal"] is not None and lookup.call_count == 0
    assert mixed["answer"].endswith(
        "I'll answer the rest of your message once you've confirmed or declined this change."
    )
    assert alone["proposal"] is not None and "rest of your message" not in alone["answer"]
