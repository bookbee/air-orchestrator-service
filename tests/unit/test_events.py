"""The SSE contract — docs/02-lld.md §4.

These tests guard the three rules a client is told it can rely on. Each is cheap to
break by accident and expensive to discover in an integrated client.
"""

from __future__ import annotations

import json

import httpx
import pytest

from air_platform.api.sse import format_event, wants_stream
from air_platform.constants import Channel, EventType
from air_platform.schemas.events import AnswerDeltaEvent, TurnStartEvent
from tests.conftest import BUSINESS_KEY, CUSTOMER_KEY, auth


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Decode a stream into ``(event name, payload)`` pairs, ignoring heartbeats."""
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        name: str | None = None
        data: str | None = None
        for line in block.splitlines():
            if line.startswith(": "):
                continue  # heartbeat comment
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data = line.removeprefix("data: ")
        if name is not None and data is not None:
            events.append((name, json.loads(data)))
    return events


async def _stream(client: httpx.AsyncClient, key: str, **body: object) -> list[tuple[str, dict]]:
    response = await client.post(
        "/v1/chat",
        headers={**auth(key), "Accept": "text/event-stream"},
        json=body,
    )
    assert response.status_code == httpx.codes.OK
    assert response.headers["content-type"].startswith("text/event-stream")
    return parse_sse(response.text)


# ── Rule 1: the frame ─────────────────────────────────────────────────────────


async def test_turn_start_and_turn_end_bracket_the_stream(client: httpx.AsyncClient) -> None:
    """A client relies on the frame rather than inferring completion from silence."""
    events = await _stream(client, CUSTOMER_KEY, message="hello")

    assert events[0][0] == EventType.TURN_START
    assert events[-1][0] == EventType.TURN_END


async def test_turn_start_carries_the_new_session_id(client: httpx.AsyncClient) -> None:
    """A caller who sent no session_id learns theirs from the first event."""
    events = await _stream(client, CUSTOMER_KEY, message="hello")

    _, payload = events[0]
    assert payload["session_id"].startswith("sess_")
    assert payload["channel"] == "customer"


# ── Rule 2: unknown names are ignorable ───────────────────────────────────────


def test_the_wire_name_and_the_payload_discriminator_agree() -> None:
    """A consumer may read either. ``format_event`` takes the name from the model, so
    the two cannot drift — which is what makes ignoring unknown names safe."""
    frame = format_event(
        TurnStartEvent(turn_id="t", session_id="s", channel=Channel.CUSTOMER)
    ).decode()
    name = next(line for line in frame.splitlines() if line.startswith("event: "))
    payload = json.loads(next(line for line in frame.splitlines() if line.startswith("data: "))[6:])

    assert name == f"event: {payload['event']}"


def test_answer_delta_is_defined_but_never_emitted_in_v1() -> None:
    """Reserved so closing the air-infra streaming gap needs no contract change.

    The model exists and frames correctly; the engine simply does not produce it. A
    client written against this contract today already ignores it, which is the whole
    point (docs/01-hld.md §5).
    """
    frame = format_event(AnswerDeltaEvent(text="tok")).decode()

    assert frame.startswith("event: answer.delta")


async def test_no_answer_delta_is_emitted(client: httpx.AsyncClient) -> None:
    events = await _stream(client, CUSTOMER_KEY, message="hello")

    assert EventType.ANSWER_DELTA not in {name for name, _ in events}


# ── Rule 3: a mid-stream failure is an event, not a status ────────────────────


async def test_a_stream_is_200_before_the_pipeline_runs(client: httpx.AsyncClient) -> None:
    """Which is exactly why `turn.end` carries the real outcome."""
    events = await _stream(client, CUSTOMER_KEY, message="hello")

    end = next(payload for name, payload in events if name == EventType.TURN_END)
    assert end["status"] == "ok"
    assert end["latency_ms"] >= 0


async def test_engine_failure_becomes_an_error_event_then_turn_end(
    client: httpx.AsyncClient, app: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Headers are already on the wire, so there is no status left to change."""
    from air_platform.engine.turn import TurnEngine

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("stage exploded")

    monkeypatch.setattr(TurnEngine, "_answer", boom)

    events = await _stream(client, CUSTOMER_KEY, message="hello")
    names = [name for name, _ in events]

    assert EventType.ERROR in names
    assert names[-1] == EventType.TURN_END
    end = next(payload for name, payload in events if name == EventType.TURN_END)
    assert end["status"] == "error"


async def test_an_error_event_never_leaks_internals(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from air_platform.engine.turn import TurnEngine

    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("connection string postgres://user:hunter2@db/air")

    monkeypatch.setattr(TurnEngine, "_answer", boom)

    events = await _stream(client, CUSTOMER_KEY, message="hello")
    error = next(payload for name, payload in events if name == EventType.ERROR)

    assert "hunter2" not in error["detail"]
    assert "postgres" not in error["detail"]
    # The same slug the equivalent pre-stream failure would carry, so a client keeps
    # one error vocabulary rather than two.
    assert error["code"] == "internal-error"


# ── Ordering ──────────────────────────────────────────────────────────────────


async def test_stages_arrive_in_pipeline_order(client: httpx.AsyncClient) -> None:
    """The order in `Stage` is the order a client sees (docs/01-hld.md §4)."""
    events = await _stream(client, CUSTOMER_KEY, message="hello")
    stages = [payload["stage"] for name, payload in events if name == EventType.STAGE]

    assert stages == [
        "guardrails_in",
        "context",
        "cache",
        "classify",
        "plan",
        "gather",
        "synthesise",
        "guardrails_out",
        "persist",
    ]


async def test_the_answer_precedes_usage_and_turn_end(client: httpx.AsyncClient) -> None:
    events = [name for name, _ in await _stream(client, CUSTOMER_KEY, message="hello")]

    assert events.index(EventType.ANSWER) < events.index(EventType.USAGE)
    assert events.index(EventType.USAGE) < events.index(EventType.TURN_END)


# ── Content negotiation ───────────────────────────────────────────────────────


def test_only_an_explicit_sse_accept_streams() -> None:
    """Absent or unrecognised means JSON, so curl and a batch caller get a body."""
    assert wants_stream("text/event-stream") is True
    assert wants_stream("text/event-stream; charset=utf-8") is True
    assert wants_stream("application/json") is False
    assert wants_stream("*/*") is False
    assert wants_stream(None) is False


async def test_both_surfaces_report_the_same_turn(client: httpx.AsyncClient) -> None:
    """One engine feeds both, so the JSON envelope is the stream, folded up."""
    streamed = await _stream(client, CUSTOMER_KEY, message="same input")
    answer_event = next(p for name, p in streamed if name == EventType.ANSWER)

    plain = await client.post(
        "/v1/chat", headers=auth(CUSTOMER_KEY), json={"message": "same input"}
    )

    # Different sessions, so the echo counter matches rather than the text exactly.
    assert plain.json()["answer"] == answer_event["text"]
    assert plain.json()["routes"] == ["direct"]


async def test_the_stream_sets_the_no_buffering_header(client: httpx.AsyncClient) -> None:
    """Without it an nginx in front delivers the whole turn at once, silently."""
    response = await client.post(
        "/v1/chat",
        headers={**auth(CUSTOMER_KEY), "Accept": "text/event-stream"},
        json={"message": "hello"},
    )

    assert response.headers["X-Accel-Buffering"] == "no"
    assert response.headers["Cache-Control"] == "no-cache"


async def test_the_business_channel_streams_the_same_contract(
    client: httpx.AsyncClient,
) -> None:
    """Same events, same frame — the channel changes the profile, not the protocol."""
    response = await client.post(
        "/v1/query",
        headers={**auth(BUSINESS_KEY), "Accept": "text/event-stream"},
        json={"query": "revenue"},
    )
    events = parse_sse(response.text)

    assert events[0][0] == EventType.TURN_START
    assert events[-1][0] == EventType.TURN_END
    answer = next(p for name, p in events if name == EventType.ANSWER)
    assert answer["structured"] is not None
