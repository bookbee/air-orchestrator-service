"""Streaming the answer as ``answer.delta`` — spec 0004 (``REQ-STRM-*``).

Each test replaces ``state.model_gateway.stream`` with a :class:`ScriptedStream`,
which replays a script through a real ``GatewayStream``, and reads the SSE response
as a client would. The client-level half (SSE parsing, pre-start refusals, closing
the connection) lives in ``test_model_gateway_client.py``; the redactor's
exhaustive split-point proof lives in ``test_guardrails_pii.py``.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI

from air_orchestrator_service.clients.model_gateway import ModelGatewayError
from air_orchestrator_service.constants import EventType
from air_orchestrator_service.engine.turn import _DEGRADED_ANSWER, _PROVIDER_REFUSAL
from tests.conftest import (
    CUSTOMER_KEY,
    HANG,
    ScriptedStream,
    auth,
    canned_summary,
    scripted_stream,
)
from tests.unit.test_events import parse_sse

_SSE = {"Accept": "text/event-stream"}


def _install(app: FastAPI, stream: ScriptedStream) -> ScriptedStream:
    from air_orchestrator_service.api.deps import STATE_ATTR

    getattr(app.state, STATE_ATTR).model_gateway.stream = stream
    return stream


async def _turn(client: httpx.AsyncClient, **body: Any) -> list[tuple[str, dict]]:
    response = await client.post(
        "/v1/chat", headers={**auth(CUSTOMER_KEY), **_SSE}, json={"message": "hi", **body}
    )
    assert response.status_code == 200
    return parse_sse(response.text)


def _deltas(events: list[tuple[str, dict]]) -> list[str]:
    return [payload["text"] for name, payload in events if name == EventType.ANSWER_DELTA]


def _one(events: list[tuple[str, dict]], name: str) -> dict:
    return next(payload for event, payload in events if event == name)


_LONG = (
    "Your parcel left the depot this morning and is with the courier now. "
    "It should reach you before six this evening, and you will get a text "
    "message when the driver is about thirty minutes away from your door."
)


def _words() -> list[str]:
    """``_LONG`` as token-sized chunks that concatenate back to it exactly."""
    words = _LONG.split(" ")
    return [w + " " for w in words[:-1]] + [words[-1]]


async def test_deltas_arrive_before_the_answer(app: FastAPI, client: httpx.AsyncClient) -> None:
    """REQ-STRM-001: more than one delta for a long answer, every one before `answer`."""
    _install(app, scripted_stream(*_words(), canned_summary()))

    events = await _turn(client)
    names = [name for name, _ in events]
    delta_positions = [i for i, name in enumerate(names) if name == EventType.ANSWER_DELTA]

    assert len(delta_positions) > 1
    assert max(delta_positions) < names.index(EventType.ANSWER)


async def test_deltas_concatenate_to_the_answer(app: FastAPI, client: httpx.AsyncClient) -> None:
    """REQ-STRM-002."""
    _install(app, scripted_stream(*_words(), canned_summary()))

    events = await _turn(client)

    assert "".join(_deltas(events)) == _one(events, EventType.ANSWER)["text"] == _LONG
    assert _one(events, EventType.TURN_END)["status"] == "ok"


async def test_a_phone_number_split_across_deltas_never_streams(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """REQ-STRM-003 / AC-1, end to end through the engine."""
    _install(app, scripted_stream("Call me on 555 12", "3 4567 tomorrow.", canned_summary()))

    events = await _turn(client)
    streamed = "".join(_deltas(events))

    for fragment in ("555 12", "3 4567", "555 123 4567"):
        assert fragment not in streamed
    assert streamed == _one(events, EventType.ANSWER)["text"]
    assert "4567" not in streamed


async def test_a_refusal_after_deltas_retracts_them(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """REQ-STRM-004 / AC-2: the verdict arrives last; `answer` overrules the deltas."""
    _install(app, scripted_stream(_LONG, " and then", canned_summary(refusal=True)))

    events = await _turn(client)
    answer = _one(events, EventType.ANSWER)

    assert _deltas(events)  # some text did stream before the verdict…
    assert answer["refusal"] is True  # …and the authoritative answer retracts it
    assert answer["text"] == _PROVIDER_REFUSAL
    assert _one(events, EventType.TURN_END)["status"] == "refused"


async def test_a_mid_stream_error_degrades_the_turn(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """REQ-STRM-005 / AC-3: a stream that broke after it started degrades, naming
    the gateway — it is not an `error` event, since nothing unexpected broke."""
    broke = ModelGatewayError("stream interrupted", retryable=True, code="provider_error")
    _install(app, scripted_stream(_LONG, broke))

    response = await client.post(
        "/v1/chat", headers={**auth(CUSTOMER_KEY), **_SSE}, json={"message": "hi"}
    )
    events = parse_sse(response.text)

    assert EventType.ERROR not in {name for name, _ in events}
    assert _one(events, EventType.ANSWER)["text"] == _DEGRADED_ANSWER
    assert _one(events, EventType.TURN_END)["status"] == "degraded"
    synthesise = next(
        p for name, p in events if name == EventType.STAGE and p["stage"] == "synthesise"
    )
    assert synthesise["status"] == "degraded"
    assert synthesise["downstream"] == "air-model-gateway"


async def test_a_stream_past_the_deadline_degrades_without_blaming_the_gateway(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """REQ-STRM-006: the deadline bounds the whole stream, and closing it stops the
    producer (the upstream connection — REQ-STRM-007 at the engine level)."""
    stream = _install(app, scripted_stream("Working on it ", HANG))

    events = await _turn(client, options={"deadline_ms": 200})

    assert _one(events, EventType.ANSWER)["text"] == _DEGRADED_ANSWER
    assert _one(events, EventType.TURN_END)["status"] == "degraded"
    synthesise = next(
        p for name, p in events if name == EventType.STAGE and p["stage"] == "synthesise"
    )
    assert synthesise["status"] == "degraded"
    assert synthesise.get("downstream") is None
    assert stream.closed is True


async def test_a_json_turn_makes_no_streaming_call(app: FastAPI, client: httpx.AsyncClient) -> None:
    """REQ-STRM-008: a JSON caller keeps the non-streaming call and its failover."""
    stream = _install(app, scripted_stream("unused", canned_summary()))

    response = await client.post("/v1/chat", headers=auth(CUSTOMER_KEY), json={"message": "hi"})

    assert response.status_code == 200
    assert stream.calls == 0


async def test_the_session_stores_the_answer_not_the_raw_stream(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """REQ-STRM-010: what is persisted is the guardrailed answer."""
    _install(app, scripted_stream("Call me on 555 12", "3 4567 tomorrow.", canned_summary()))

    events = await _turn(client)
    session_id = _one(events, EventType.TURN_START)["session_id"]
    answer = _one(events, EventType.ANSWER)["text"]

    view = await client.get(f"/v1/sessions/{session_id}", headers=auth(CUSTOMER_KEY))
    stored = [t["content"] for t in view.json()["turns"] if t["role"] == "assistant"]

    assert stored == [answer]
    assert "4567" not in stored[0]
