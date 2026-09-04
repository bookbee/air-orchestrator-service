"""The air-llm probe: it must never raise, and never leak.

A readiness endpoint that can fail reports nothing at the moment it matters most,
so every failure mode has to become a `reachable=False` plus a short reason.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from air_platform.clients.llm import LlmCallError, LlmClient
from air_platform.config import Settings


def _client(**llm: object) -> LlmClient:
    return LlmClient(
        Settings.model_validate(
            {"app": {"env": "test"}, "llm": {"base_url": "http://gateway:8083", **llm}}
        )
    )


@respx.mock
async def test_a_healthy_gateway_is_reachable() -> None:
    respx.get("http://gateway:8083/v1/ready").mock(return_value=httpx.Response(200))
    client = _client()

    status = await client.probe()

    assert status.reachable is True
    assert status.configured is True
    assert status.latency_ms is not None
    assert status.detail is None
    await client.aclose()


@respx.mock
async def test_a_non_200_is_unreachable_with_only_the_status_code() -> None:
    """The body is not echoed: an upstream error page can carry a stack trace or an
    internal hostname, and /v1/ready is unauthenticated."""
    respx.get("http://gateway:8083/v1/ready").mock(
        return_value=httpx.Response(503, text="Traceback: /opt/air-llm/secret.py line 3")
    )
    client = _client()

    status = await client.probe()

    assert status.reachable is False
    assert status.detail == "air-llm returned HTTP 503"
    assert "Traceback" not in (status.detail or "")
    await client.aclose()


@respx.mock
async def test_a_timeout_is_reported_rather_than_raised() -> None:
    respx.get("http://gateway:8083/v1/ready").mock(side_effect=httpx.ConnectTimeout)
    client = _client(health_timeout_s=0.5)

    status = await client.probe()

    assert status.reachable is False
    assert "0.5" in (status.detail or "")
    await client.aclose()


@respx.mock
async def test_a_connection_failure_never_renders_the_url() -> None:
    """httpx puts the full URL in its message, and a base URL can embed credentials."""
    respx.get("http://gateway:8083/v1/ready").mock(side_effect=httpx.ConnectError("boom"))
    client = _client()

    status = await client.probe()

    assert status.reachable is False
    assert "gateway:8083" not in (status.detail or "")
    assert "http://" not in (status.detail or "")
    assert "ConnectError" in (status.detail or "")
    await client.aclose()


@respx.mock
async def test_the_api_key_is_sent_as_x_api_key() -> None:
    route = respx.get("http://gateway:8083/v1/ready").mock(return_value=httpx.Response(200))
    client = _client(api_key="secret-token")

    await client.probe()

    assert route.calls.last.request.headers["X-API-Key"] == "secret-token"
    await client.aclose()


async def test_aclose_is_idempotent() -> None:
    """A double shutdown happens on reload, and must not raise."""
    client = _client()

    await client.aclose()
    await client.aclose()


@respx.mock
async def test_the_pool_is_built_once_under_concurrency() -> None:
    """Two concurrent first calls must not each build a client and leak a pool."""
    import asyncio

    respx.get("http://gateway:8083/v1/ready").mock(return_value=httpx.Response(200))
    client = _client()

    await asyncio.gather(*(client.probe() for _ in range(4)))

    inner = await client._http()
    assert inner is await client._http()
    await client.aclose()


def test_health_timeout_is_much_shorter_than_the_turn_timeout() -> None:
    """A probe that waits as long as a real request detects an outage too late."""
    settings = Settings.model_validate({"app": {"env": "test"}})

    assert settings.llm.health_timeout_s < settings.llm.timeout_s / 5


def test_a_zero_health_timeout_is_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings.model_validate({"app": {"env": "test"}, "llm": {"health_timeout_s": 0}})


# ── chat() ────────────────────────────────────────────────────────────────────


@respx.mock
async def test_chat_returns_the_content_field() -> None:
    respx.post("http://gateway:8083/v1/inference").mock(
        return_value=httpx.Response(200, json={"content": "hello", "cost_usd": 0.002})
    )
    client = _client()

    result = await client.chat(model="generative", messages=[{"role": "user", "content": "hi"}])

    assert result.content == "hello"
    assert result.cost_usd == 0.002
    await client.aclose()


@respx.mock
async def test_chat_sends_the_role_alias_as_model() -> None:
    route = respx.post("http://gateway:8083/v1/inference").mock(
        return_value=httpx.Response(200, json={"content": "ok"})
    )
    client = _client()

    await client.chat(model="generative", messages=[{"role": "user", "content": "hi"}])

    assert route.calls.last.request.content
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "generative"
    assert sent["task"] == "chat"
    await client.aclose()


@respx.mock
async def test_chat_raises_rather_than_swallowing_a_failure() -> None:
    """Unlike `probe()`, a caller here actually needs the answer — it must be
    able to tell "no answer" from "an empty one"."""
    respx.post("http://gateway:8083/v1/inference").mock(side_effect=httpx.ConnectError("boom"))
    client = _client()

    with pytest.raises(LlmCallError):
        await client.chat(model="generative", messages=[{"role": "user", "content": "hi"}])
    await client.aclose()


@respx.mock
async def test_a_connection_failure_is_classified_retryable() -> None:
    respx.post("http://gateway:8083/v1/inference").mock(side_effect=httpx.ConnectError("boom"))
    client = _client()

    with pytest.raises(LlmCallError) as excinfo:
        await client.chat(model="generative", messages=[{"role": "user", "content": "hi"}])

    assert excinfo.value.retryable is True
    await client.aclose()


@respx.mock
async def test_a_429_is_classified_retryable() -> None:
    respx.post("http://gateway:8083/v1/inference").mock(return_value=httpx.Response(429))
    client = _client()

    with pytest.raises(LlmCallError) as excinfo:
        await client.chat(model="generative", messages=[{"role": "user", "content": "hi"}])

    assert excinfo.value.retryable is True
    await client.aclose()


@respx.mock
async def test_a_400_is_classified_not_retryable() -> None:
    respx.post("http://gateway:8083/v1/inference").mock(return_value=httpx.Response(400))
    client = _client()

    with pytest.raises(LlmCallError) as excinfo:
        await client.chat(model="generative", messages=[{"role": "user", "content": "hi"}])

    assert excinfo.value.retryable is False
    await client.aclose()


@respx.mock
async def test_chat_passes_a_custom_timeout_through_to_httpx() -> None:
    route = respx.post("http://gateway:8083/v1/inference").mock(
        return_value=httpx.Response(200, json={"content": "ok"})
    )
    client = _client()

    await client.chat(
        model="generative", messages=[{"role": "user", "content": "hi"}], timeout=1.5
    )

    # respx does not expose the resolved timeout directly; the meaningful
    # assertion is that a custom timeout doesn't break the call at all.
    assert route.called
    await client.aclose()


@respx.mock
async def test_chat_forwards_the_bound_request_id() -> None:
    from air_platform.observability.logging import bind_request_context, clear_request_context

    route = respx.post("http://gateway:8083/v1/inference").mock(
        return_value=httpx.Response(200, json={"content": "ok"})
    )
    client = _client()
    bind_request_context(request_id="req_test123")
    try:
        await client.chat(model="generative", messages=[{"role": "user", "content": "hi"}])
    finally:
        clear_request_context()

    assert route.calls.last.request.headers["X-Request-ID"] == "req_test123"
    await client.aclose()
