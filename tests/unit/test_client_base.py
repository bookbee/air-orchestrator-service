"""The shared optional-downstream client — spec 0010 (``REQ-CLB-*``).

Driven against a respx service at an ``.invalid`` host, with a fake clock for the
breaker, so the cooldown is stepped over rather than slept through.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from air_orchestrator_service.clients.base import (
    BreakerState,
    DownstreamClient,
    DownstreamError,
)
from air_orchestrator_service.config import ServiceSettings
from air_orchestrator_service.constants import HEADER_API_KEY, HEADER_REQUEST_ID, DownstreamService
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.observability.logging import (
    bind_request_context,
    clear_request_context,
)

_BASE = "http://air-rag-service.invalid:8087"
_URL = f"{_BASE}/v1/thing"
_KEY = "airr_secret_value_never_rendered"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
async def client(clock: _Clock) -> AsyncIterator[DownstreamClient]:
    config = ServiceSettings(
        enabled=True,
        base_url=_BASE,
        api_key=SecretStr(_KEY),
        timeout_ms=1000,
        breaker_threshold=3,
        breaker_cooldown_s=30,
    )
    downstream = DownstreamClient(DownstreamService.RAG, config, clock=clock)
    yield downstream
    await downstream.aclose()


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


async def _call(client: DownstreamClient) -> Any:
    return await client.post_json("/v1/thing", {"q": 1}, timeout_s=1.0, parse=lambda body: body)


def _count(outcome: str) -> float:
    value = metrics.REGISTRY.get_sample_value(
        "air_orchestrator_service_downstream_calls_total",
        {"service": DownstreamService.RAG.value, "outcome": outcome},
    )
    return value or 0.0


async def test_a_call_sends_the_key_and_request_id_once(
    client: DownstreamClient, mock: respx.MockRouter
) -> None:
    """REQ-CLB-001."""
    route = mock.post(_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    bind_request_context(request_id="req_abc")
    try:
        assert await _call(client) == {"ok": True}
    finally:
        clear_request_context()

    assert route.call_count == 1
    sent = route.calls.last.request
    assert sent.headers[HEADER_API_KEY] == _KEY
    assert sent.headers[HEADER_REQUEST_ID] == "req_abc"


async def test_a_failure_is_attempted_once(
    client: DownstreamClient, mock: respx.MockRouter
) -> None:
    """REQ-CLB-001: no retry."""
    route = mock.post(_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(DownstreamError):
        await _call(client)

    assert route.call_count == 1


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.ConnectError("refused"), "unavailable"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.Response(503), "error"),
        (httpx.Response(422, json={"detail": "bad"}), "error"),
        (httpx.Response(200, text="not json"), "error"),
    ],
)
async def test_each_failure_is_one_reason_and_one_metric(
    client: DownstreamClient, mock: respx.MockRouter, response: Any, reason: str
) -> None:
    """REQ-CLB-002."""
    if isinstance(response, Exception):
        mock.post(_URL).mock(side_effect=response)
    else:
        mock.post(_URL).mock(return_value=response)
    before = _count(reason)

    with pytest.raises(DownstreamError) as caught:
        await _call(client)

    assert caught.value.reason == reason
    assert caught.value.service is DownstreamService.RAG
    assert _count(reason) == before + 1


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.ConnectError("refused"), "unavailable"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.Response(503), "error"),
        (httpx.Response(404, json={"detail": "bad"}), "error"),
        (httpx.Response(200, text="not json"), "error"),
    ],
)
async def test_get_json_fails_with_the_same_four_reasons(
    client: DownstreamClient, mock: respx.MockRouter, response: Any, reason: str
) -> None:
    """REQ-CLB-002 holds for a GET: spec 0012 reads air-tools-gateway's inventory this way."""
    if isinstance(response, Exception):
        route = mock.get(_URL).mock(side_effect=response)
    else:
        route = mock.get(_URL).mock(return_value=response)
    before = _count(reason)

    with pytest.raises(DownstreamError) as caught:
        await client.get_json("/v1/thing", timeout_s=1.0, parse=lambda body: body)

    assert caught.value.reason == reason
    assert _count(reason) == before + 1
    assert route.call_count == 1
    assert _URL not in str(caught.value)


async def test_get_json_sends_the_key_and_returns_the_parsed_body(
    client: DownstreamClient, mock: respx.MockRouter
) -> None:
    route = mock.get(_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    assert await client.get_json("/v1/thing", timeout_s=1.0, parse=lambda body: body) == {
        "ok": True
    }
    assert route.calls.last.request.headers[HEADER_API_KEY] == _KEY
    assert route.calls.last.request.content == b""


@pytest.mark.parametrize("reserved", [HEADER_API_KEY, "x-api-key", HEADER_REQUEST_ID])
async def test_extra_headers_are_sent_but_never_replace_the_key(
    client: DownstreamClient, mock: respx.MockRouter, reserved: str
) -> None:
    """A call may add ``Idempotency-Key``; it may not choose the key or the request id.

    Refused before any request, so the breaker and the metric see nothing.
    """
    route = mock.post(_URL).mock(return_value=httpx.Response(200, json={}))

    await client.post_json(
        "/v1/thing",
        {},
        timeout_s=1.0,
        parse=lambda body: body,
        headers={"Idempotency-Key": "airo-prop_1"},
    )
    sent = route.calls.last.request.headers
    assert sent["Idempotency-Key"] == "airo-prop_1"
    assert sent[HEADER_API_KEY] == _KEY

    with pytest.raises(ValueError, match="api_key="):
        await client.post_json(
            "/v1/thing",
            {},
            timeout_s=1.0,
            parse=lambda body: body,
            headers={reserved: "airr_chosen_by_the_caller"},
        )
    assert route.call_count == 1


async def test_a_body_that_fails_parsing_is_an_error(
    client: DownstreamClient, mock: respx.MockRouter
) -> None:
    """REQ-CLB-002: ``parse`` raising is an ``error``, inside the breaker's verdict."""
    mock.post(_URL).mock(return_value=httpx.Response(200, json={"x": 1}))

    def parse(_: Any) -> Any:
        raise ValueError("wrong shape")

    with pytest.raises(DownstreamError) as caught:
        await client.post_json("/v1/thing", {}, timeout_s=1.0, parse=parse)

    assert caught.value.reason == "error"
    assert client.breaker._failures == 1


async def test_an_error_never_carries_the_url_or_body(
    client: DownstreamClient, mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
) -> None:
    """REQ-CLB-003."""
    secret_body = {"detail": f"stack trace mentioning {_KEY} and {_BASE}"}
    mock.post(_URL).mock(return_value=httpx.Response(500, json=secret_body))

    with caplog.at_level(logging.DEBUG), pytest.raises(DownstreamError) as caught:
        await _call(client)

    for text in (str(caught.value), repr(client).replace(_BASE, ""), caplog.text):
        assert _KEY not in text
        assert "stack trace" not in text
    assert "invalid" not in str(caught.value)
    assert str(caught.value) == "air-rag returned HTTP 500"


async def test_the_breaker_opens_after_the_threshold(
    client: DownstreamClient, mock: respx.MockRouter
) -> None:
    """REQ-CLB-004: three failures open it, and the fourth call makes no request."""
    route = mock.post(_URL).mock(side_effect=httpx.ConnectError("refused"))
    before = _count("breaker_open")

    for _ in range(3):
        with pytest.raises(DownstreamError):
            await _call(client)
    with pytest.raises(DownstreamError) as caught:
        await _call(client)

    assert route.call_count == 3
    assert caught.value.reason == "breaker_open"
    assert client.breaker.state is BreakerState.OPEN
    assert _count("breaker_open") == before + 1


async def test_a_success_resets_the_count(client: DownstreamClient, mock: respx.MockRouter) -> None:
    """REQ-CLB-004: *consecutive* failures."""
    mock.post(_URL).mock(
        side_effect=[
            httpx.ConnectError("x"),
            httpx.ConnectError("x"),
            httpx.Response(200, json={}),
            httpx.ConnectError("x"),
            httpx.ConnectError("x"),
        ]
    )
    for _ in range(5):
        with contextlib.suppress(DownstreamError):
            await _call(client)

    assert client.breaker.state is BreakerState.CLOSED


async def test_after_the_cooldown_one_probe_decides(
    client: DownstreamClient, mock: respx.MockRouter, clock: _Clock
) -> None:
    """REQ-CLB-005: a failed probe reopens; a successful one closes."""
    route = mock.post(_URL).mock(side_effect=httpx.ConnectError("refused"))
    for _ in range(3):
        with pytest.raises(DownstreamError):
            await _call(client)

    clock.now += 31
    assert client.breaker.state is BreakerState.HALF_OPEN
    with pytest.raises(DownstreamError) as failed_probe:
        await _call(client)
    assert failed_probe.value.reason == "unavailable"
    assert route.call_count == 4
    assert client.breaker.state is BreakerState.OPEN

    clock.now += 31
    route.mock(side_effect=None, return_value=httpx.Response(200, json={"ok": True}))
    assert await _call(client) == {"ok": True}
    assert client.breaker.state is BreakerState.CLOSED


async def test_a_cancelled_call_is_not_a_failure(
    client: DownstreamClient, mock: respx.MockRouter
) -> None:
    """REQ-CLB-006."""

    async def hang(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return httpx.Response(200, json={})

    mock.post(_URL).mock(side_effect=hang)

    task = asyncio.create_task(_call(client))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client.breaker._failures == 0
    assert client.breaker.state is BreakerState.CLOSED


async def test_an_open_breaker_is_reported_without_a_probe(
    client: DownstreamClient, mock: respx.MockRouter
) -> None:
    """REQ-CLB-007: the health probe is not sent while the breaker is open."""
    mock.post(_URL).mock(side_effect=httpx.ConnectError("refused"))
    health = mock.get(f"{_BASE}/v1/health").mock(return_value=httpx.Response(200))
    for _ in range(3):
        with pytest.raises(DownstreamError):
            await _call(client)

    status = await client.probe()

    assert status.reachable is False
    assert status.detail == "circuit open"
    assert health.call_count == 0


async def test_a_refusal_code_is_an_answer_and_nothing_else_is(
    client: DownstreamClient, mock: respx.MockRouter
) -> None:
    """Spec 0012 ``REQ-TGW-028`` at the base: a named problem code raises
    ``refused`` with the code, records ``refused`` and leaves the breaker closed, however
    many arrive. An unnamed code, or a body that is not JSON, is still ``error``."""
    mock.post(_URL).mock(
        side_effect=[httpx.Response(422, json={"code": "invalid_state"})] * 5
        + [httpx.Response(422, json={"code": "other"}), httpx.Response(422, text="no")]
    )
    before = _count("refused")

    for _ in range(5):
        with pytest.raises(DownstreamError) as caught:
            await client.post_json(
                "/v1/thing",
                {},
                timeout_s=1.0,
                parse=lambda body: body,
                refusal_codes=frozenset({"invalid_state"}),
            )
        assert (caught.value.reason, caught.value.code) == ("refused", "invalid_state")
    for _ in range(2):
        with pytest.raises(DownstreamError) as caught:
            await client.post_json(
                "/v1/thing",
                {},
                timeout_s=1.0,
                parse=lambda body: body,
                refusal_codes=frozenset({"invalid_state"}),
            )
        assert (caught.value.reason, caught.value.code) == ("error", None)

    assert _count("refused") == before + 5
    assert client.breaker.state is BreakerState.CLOSED
