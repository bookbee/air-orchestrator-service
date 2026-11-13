"""The air-model-gateway client (spec 0003).

Two halves with opposite failure contracts. The readiness probe must never raise and
never leak — a readiness endpoint that can fail reports nothing at the moment it matters
most. ``generate`` must raise, classified, because the caller needs to tell "no answer"
from "an empty one".

Every HTTP call is intercepted by respx (an unmatched one fails the test), so nothing
here depends on what runs on the developer's machine.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError

from air_orchestrator_service.clients.model_gateway import (
    ModelGatewayClient,
    ModelGatewayError,
    StreamDelta,
    StreamDone,
)
from air_orchestrator_service.config import Settings

BASE = "http://gateway:8083"
MESSAGES = [{"role": "user", "content": "hi"}]

#: Exactly the keys air-model-gateway's `GenerateInput` defines. Its body model is
#: `extra="forbid"`: anything outside this set is a 422 on every call.
GATEWAY_GENERATE_FIELDS = {
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


def _client(**gateway: object) -> ModelGatewayClient:
    return ModelGatewayClient(
        Settings.model_validate(
            {"app": {"env": "test"}, "model_gateway": {"base_url": BASE, **gateway}}
        )
    )


def _capabilities(name: str = "cloud", *, allowed: bool = True, available: bool = True) -> Any:
    return {"generate": {name: {"allowed": allowed, "available": available, "chain": []}}}


def _mock_probe(
    *, ready: int = 200, caps_status: int = 200, caps: Any = None
) -> tuple[respx.Route, respx.Route]:
    ready_route = respx.get(f"{BASE}/v1/ready").mock(return_value=httpx.Response(ready))
    caps_route = respx.get(f"{BASE}/v1/capabilities").mock(
        return_value=httpx.Response(caps_status, json=_capabilities() if caps is None else caps)
    )
    return ready_route, caps_route


# ── Readiness ─────────────────────────────────────────────────────────────────


@respx.mock
async def test_a_gateway_that_serves_this_service_is_reachable() -> None:
    _mock_probe()
    client = _client(api_key="t")

    status = await client.probe()

    assert status.reachable is True
    assert status.configured is True
    assert status.latency_ms is not None
    assert status.detail is None
    await client.aclose()


@respx.mock
async def test_a_non_200_is_unready_with_only_the_status_code() -> None:
    """REQ-MGW-012. The body is not echoed: an upstream error page can carry a stack
    trace or an internal hostname, and /v1/ready is unauthenticated."""
    respx.get(f"{BASE}/v1/ready").mock(
        return_value=httpx.Response(503, text="Traceback: /opt/gateway/secret.py line 3")
    )
    respx.get(f"{BASE}/v1/capabilities").mock(return_value=httpx.Response(200, json={}))
    client = _client()

    status = await client.probe()

    assert status.reachable is False
    assert "HTTP 503" in (status.detail or "")
    assert "Traceback" not in (status.detail or "")
    await client.aclose()


@respx.mock
async def test_probe_is_unready_when_the_token_is_rejected() -> None:
    """REQ-MGW-010. The gateway's own /v1/ready is green for any caller; the token is
    only proved by an authenticated call. This is the case that hid the contract
    break: a healthy gateway that cannot serve *this* service."""
    _mock_probe(caps_status=401, caps={"code": "unauthorized"})
    client = _client(api_key="wrong")

    status = await client.probe()

    assert status.reachable is False
    assert "rejected this service's token" in (status.detail or "")
    await client.aclose()


@respx.mock
async def test_a_rejected_token_never_renders_the_token() -> None:
    """REQ-MGW-012 / Article IX."""
    _mock_probe(caps_status=401)
    client = _client(api_key="sk-live-orchestrator-token")

    status = await client.probe()

    assert "sk-live-orchestrator-token" not in status.model_dump_json()
    await client.aclose()


@respx.mock
async def test_probe_is_unready_when_the_class_is_not_allowed() -> None:
    """REQ-MGW-011: a 403 on every call, the moment a turn arrives."""
    _mock_probe(caps=_capabilities(allowed=False))
    client = _client(api_key="t")

    status = await client.probe()

    assert status.reachable is False
    assert "'cloud' is not granted" in (status.detail or "")
    await client.aclose()


@respx.mock
async def test_probe_is_unready_when_the_class_is_not_available() -> None:
    """REQ-MGW-011: granted, but no provider behind it in this deployment — a laptop
    gateway without cloud keys, asked for `cloud`."""
    _mock_probe(caps=_capabilities(available=False))
    client = _client(api_key="t")

    status = await client.probe()

    assert status.reachable is False
    assert "no configured provider" in (status.detail or "")
    await client.aclose()


@respx.mock
async def test_probe_checks_the_configured_class_not_a_hardcoded_one() -> None:
    _mock_probe(caps={"generate": {"local": {"allowed": True, "available": True}}})
    client = _client(api_key="t", model_class="local")

    status = await client.probe()

    assert status.reachable is True
    await client.aclose()


@respx.mock
async def test_a_capabilities_body_without_the_class_is_unready() -> None:
    _mock_probe(caps={"generate": {}})
    client = _client(api_key="t")

    status = await client.probe()

    assert status.reachable is False
    assert "not offered" in (status.detail or "")
    await client.aclose()


@respx.mock
async def test_a_timeout_is_reported_rather_than_raised() -> None:
    respx.get(f"{BASE}/v1/ready").mock(side_effect=httpx.ConnectTimeout)
    respx.get(f"{BASE}/v1/capabilities").mock(side_effect=httpx.ConnectTimeout)
    client = _client(health_timeout_s=0.5)

    status = await client.probe()

    assert status.reachable is False
    assert "0.5" in (status.detail or "")
    await client.aclose()


@respx.mock
async def test_a_connection_failure_never_renders_the_url() -> None:
    """REQ-MGW-012. httpx puts the full URL in its message, and a base URL can embed
    credentials."""
    respx.get(f"{BASE}/v1/ready").mock(side_effect=httpx.ConnectError("boom"))
    respx.get(f"{BASE}/v1/capabilities").mock(side_effect=httpx.ConnectError("boom"))
    client = _client()

    status = await client.probe()

    assert status.reachable is False
    assert "gateway:8083" not in (status.detail or "")
    assert "http://" not in (status.detail or "")
    assert "ConnectError" in (status.detail or "")
    await client.aclose()


@respx.mock
async def test_the_readiness_probe_sends_the_token() -> None:
    """REQ-MGW-006: the probe's job is to prove the token works."""
    _, caps_route = _mock_probe()
    client = _client(api_key="secret-token")

    await client.probe()

    assert caps_route.calls.last.request.headers["X-API-Key"] == "secret-token"
    await client.aclose()


async def test_aclose_is_idempotent() -> None:
    """A double shutdown happens on reload, and must not raise."""
    client = _client()

    await client.aclose()
    await client.aclose()


@respx.mock
async def test_the_pool_is_built_once_under_concurrency() -> None:
    """Two concurrent first calls must not each build a client and leak a pool."""
    _mock_probe()
    client = _client()

    await asyncio.gather(*(client.probe() for _ in range(4)))

    inner = await client._http()
    assert inner is await client._http()
    await client.aclose()


def test_health_timeout_is_much_shorter_than_the_turn_timeout() -> None:
    """A probe that waits as long as a real request detects an outage too late."""
    settings = Settings.model_validate({"app": {"env": "test"}})

    assert settings.model_gateway.health_timeout_s < settings.model_gateway.timeout_s / 5


def test_a_zero_health_timeout_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"app": {"env": "test"}, "model_gateway": {"health_timeout_s": 0}})


# ── generate() ────────────────────────────────────────────────────────────────


@respx.mock
async def test_generate_posts_to_v1_generate() -> None:
    """REQ-MGW-001: `/v1/inference` is a 404 on the current gateway."""
    route = respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(200, json={"content": "hello", "cost_usd": 0.002})
    )
    client = _client()

    result = await client.generate(messages=MESSAGES)

    assert route.called
    assert result.content == "hello"
    assert result.cost_usd == 0.002
    await client.aclose()


@respx.mock
async def test_generate_body_carries_only_gateway_contract_fields() -> None:
    """REQ-MGW-002: `task` broke every call; any other stray field would too."""
    route = respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(200, json={"content": "{}"})
    )
    client = _client()

    await client.generate(
        messages=MESSAGES, json_schema={"type": "object"}, schema_name="business_query"
    )

    sent = json.loads(route.calls.last.request.content)
    assert set(sent) <= GATEWAY_GENERATE_FIELDS
    assert "task" not in sent
    await client.aclose()


@respx.mock
async def test_generate_sends_the_configured_class_as_model() -> None:
    """REQ-MGW-003: a class, never an alias or a model name."""
    route = respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(200, json={"content": "ok"})
    )
    client = _client(model_class="local")

    await client.generate(messages=MESSAGES)

    assert json.loads(route.calls.last.request.content)["model"] == "local"
    await client.aclose()


@respx.mock
async def test_the_token_is_sent_as_x_api_key() -> None:
    """REQ-MGW-006."""
    route = respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(200, json={"content": "ok"})
    )
    client = _client(api_key="secret-token")

    await client.generate(messages=MESSAGES)

    assert route.calls.last.request.headers["X-API-Key"] == "secret-token"
    await client.aclose()


@respx.mock
async def test_generate_reads_refusal_and_finish_reason() -> None:
    respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(
            200, json={"content": "", "refusal": True, "finish_reason": "refusal"}
        )
    )
    client = _client()

    result = await client.generate(messages=MESSAGES)

    assert result.refusal is True
    assert result.cut_off is False
    await client.aclose()


@respx.mock
async def test_generate_raises_rather_than_swallowing_a_failure() -> None:
    """Unlike `probe()`, a caller here actually needs the answer."""
    respx.post(f"{BASE}/v1/generate").mock(side_effect=httpx.ConnectError("boom"))
    client = _client()

    with pytest.raises(ModelGatewayError):
        await client.generate(messages=MESSAGES)
    await client.aclose()


@respx.mock
async def test_a_connection_failure_is_classified_retryable() -> None:
    """REQ-MGW-007."""
    respx.post(f"{BASE}/v1/generate").mock(side_effect=httpx.ConnectError("boom"))
    client = _client()

    with pytest.raises(ModelGatewayError) as caught:
        await client.generate(messages=MESSAGES)

    assert caught.value.retryable is True
    assert caught.value.code is None
    await client.aclose()


@respx.mock
async def test_rate_limited_is_retryable() -> None:
    """REQ-MGW-007: a throttle clears with backoff."""
    respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(429, json={"code": "rate_limited", "status": 429})
    )
    client = _client()

    with pytest.raises(ModelGatewayError) as caught:
        await client.generate(messages=MESSAGES)

    assert caught.value.retryable is True
    assert caught.value.code == "rate_limited"
    await client.aclose()


@respx.mock
async def test_budget_exceeded_is_not_retryable_and_keeps_its_code() -> None:
    """REQ-MGW-007: same status as a throttle, opposite meaning — it clears at the next
    UTC day, and retrying it only adds load."""
    respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(429, json={"code": "budget_exceeded", "status": 429})
    )
    client = _client()

    with pytest.raises(ModelGatewayError) as caught:
        await client.generate(messages=MESSAGES)

    assert caught.value.retryable is False
    assert caught.value.code == "budget_exceeded"
    assert caught.value.status == 429
    await client.aclose()


@respx.mock
async def test_a_422_is_not_retryable() -> None:
    """REQ-MGW-007: a contract break or a provider rejecting the request — the same
    call fails the same way again."""
    respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(422, json={"code": "validation_error"})
    )
    client = _client()

    with pytest.raises(ModelGatewayError) as caught:
        await client.generate(messages=MESSAGES)

    assert caught.value.retryable is False
    await client.aclose()


@respx.mock
async def test_a_502_is_retryable() -> None:
    respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(502, json={"code": "provider_unavailable"})
    )
    client = _client()

    with pytest.raises(ModelGatewayError) as caught:
        await client.generate(messages=MESSAGES)

    assert caught.value.retryable is True
    await client.aclose()


@respx.mock
async def test_an_unexpected_code_shape_is_not_echoed() -> None:
    """The problem body is upstream-controlled; only a slug-shaped code is kept."""
    respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(400, json={"code": "Bad\nthing <script>"})
    )
    client = _client()

    with pytest.raises(ModelGatewayError) as caught:
        await client.generate(messages=MESSAGES)

    assert caught.value.code is None
    assert "script" not in str(caught.value)
    await client.aclose()


@respx.mock
async def test_generate_passes_a_custom_timeout_through_to_httpx() -> None:
    route = respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(200, json={"content": "ok"})
    )
    client = _client()

    await client.generate(messages=MESSAGES, timeout=1.5)

    assert route.calls.last.request.extensions["timeout"]["read"] == 1.5
    await client.aclose()


@respx.mock
async def test_generate_forwards_the_bound_request_id() -> None:
    """REQ-MGW-006: one id follows the turn into the gateway's audit line."""
    from air_orchestrator_service.observability.logging import (
        bind_request_context,
        clear_request_context,
    )

    route = respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(200, json={"content": "ok"})
    )
    client = _client()
    bind_request_context(request_id="req_test123")
    try:
        await client.generate(messages=MESSAGES)
    finally:
        clear_request_context()

    assert route.calls.last.request.headers["X-Request-ID"] == "req_test123"
    await client.aclose()


# ── stream() (spec 0004) ──────────────────────────────────────────────────────


def _sse(*frames: tuple[str, object]) -> bytes:
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in frames).encode()


_DONE = {
    "id": "g1",
    "model": "claude-sonnet-5",
    "provider": "anthropic",
    "usage": {"prompt_tokens": 9, "completion_tokens": 3},
    "cost_usd": 0.0004,
    "cached": False,
    "refusal": False,
    "finish_reason": "stop",
}


async def _drain(client: ModelGatewayClient) -> list[Any]:
    items: list[Any] = []
    async with client.stream(messages=MESSAGES) as stream:
        while True:
            item = await stream.next(timeout=2.0)
            items.append(item)
            if isinstance(item, StreamDone):
                return items


@respx.mock
async def test_stream_yields_deltas_then_the_summary() -> None:
    """REQ-STRM-012: framing parsed, `start` ignored, usage and cost from `done`."""
    route = respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse(
                ("start", {"id": "g1", "model": "m", "provider": "anthropic", "cached": False}),
                ("delta", {"content": "Hel"}),
                ("delta", {"content": "lo"}),
                ("done", _DONE),
            ),
        )
    )
    client = _client()

    items = await _drain(client)

    assert [i.text for i in items if isinstance(i, StreamDelta)] == ["Hel", "lo"]
    done = items[-1]
    assert isinstance(done, StreamDone)
    assert done.result.cost_usd == 0.0004
    assert done.result.usage.prompt_tokens == 9
    sent = json.loads(route.calls.last.request.content)
    assert sent["stream"] is True
    assert set(sent) <= GATEWAY_GENERATE_FIELDS
    await client.aclose()


@respx.mock
async def test_stream_error_event_raises_after_deltas() -> None:
    """REQ-STRM-012: an `error` event replaces `done`; the text so far is incomplete."""
    respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=_sse(
                ("start", {"id": "g1", "model": "m", "provider": "gemini", "cached": False}),
                ("delta", {"content": "Partial"}),
                ("error", {"code": "provider_unavailable", "detail": "went silent"}),
            ),
        )
    )
    client = _client()

    async with client.stream(messages=MESSAGES) as stream:
        first = await stream.next(timeout=2.0)
        with pytest.raises(ModelGatewayError) as caught:
            await stream.next(timeout=2.0)

    assert isinstance(first, StreamDelta)
    assert caught.value.code == "provider_unavailable"
    assert caught.value.retryable is True
    await client.aclose()


@respx.mock
async def test_a_stream_refused_before_start_is_classified() -> None:
    """REQ-STRM-009: a plain HTTP problem before the first byte, classified exactly as
    the non-streaming call — a spent budget is still not retryable."""
    respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(429, json={"code": "budget_exceeded", "status": 429})
    )
    client = _client()

    async with client.stream(messages=MESSAGES) as stream:
        with pytest.raises(ModelGatewayError) as caught:
            await stream.next(timeout=2.0)

    assert caught.value.code == "budget_exceeded"
    assert caught.value.retryable is False
    await client.aclose()


class _NeverEndingStream(httpx.AsyncByteStream):
    """Sends `start` and one delta, then hangs — a long answer still generating."""

    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> Any:
        yield _sse(("start", {"id": "g", "model": "m", "provider": "anthropic", "cached": False}))
        yield _sse(("delta", {"content": "still going"}))
        await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


@respx.mock
async def test_closing_a_stream_closes_the_gateway_connection() -> None:
    """REQ-STRM-007: closing the connection is what makes the gateway stop generating
    — and stop billing — when the turn's own client goes away or its deadline passes."""
    body = _NeverEndingStream()
    respx.post(f"{BASE}/v1/generate").mock(
        return_value=httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=body)
    )
    client = _client()

    async with client.stream(messages=MESSAGES) as stream:
        first = await stream.next(timeout=2.0)
        with pytest.raises(TimeoutError):
            await stream.next(timeout=0.05)  # the caller's deadline, not the gateway's

    assert isinstance(first, StreamDelta)
    assert body.closed is True
    await client.aclose()
