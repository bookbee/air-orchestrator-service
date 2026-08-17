"""The air-infra probe: it must never raise, and never leak.

A readiness endpoint that can fail reports nothing at the moment it matters most,
so every failure mode has to become a `reachable=False` plus a short reason.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from air_platform.clients.infra import InfraClient
from air_platform.config import Settings


def _client(**infra: object) -> InfraClient:
    return InfraClient(
        Settings.model_validate(
            {"app": {"env": "test"}, "infra": {"base_url": "http://gateway:8080", **infra}}
        )
    )


@respx.mock
async def test_a_healthy_gateway_is_reachable() -> None:
    respx.get("http://gateway:8080/v1/health").mock(return_value=httpx.Response(200))
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
    respx.get("http://gateway:8080/v1/health").mock(
        return_value=httpx.Response(500, text="Traceback: /opt/air-infra/secret.py line 3")
    )
    client = _client()

    status = await client.probe()

    assert status.reachable is False
    assert status.detail == "gateway returned HTTP 500"
    assert "Traceback" not in (status.detail or "")
    await client.aclose()


@respx.mock
async def test_a_timeout_is_reported_rather_than_raised() -> None:
    respx.get("http://gateway:8080/v1/health").mock(side_effect=httpx.ConnectTimeout)
    client = _client(health_timeout_s=0.5)

    status = await client.probe()

    assert status.reachable is False
    assert "0.5" in (status.detail or "")
    await client.aclose()


@respx.mock
async def test_a_connection_failure_never_renders_the_url() -> None:
    """httpx puts the full URL in its message, and a base URL can embed credentials."""
    respx.get("http://gateway:8080/v1/health").mock(side_effect=httpx.ConnectError("boom"))
    client = _client()

    status = await client.probe()

    assert status.reachable is False
    assert "gateway:8080" not in (status.detail or "")
    assert "http://" not in (status.detail or "")
    assert "ConnectError" in (status.detail or "")
    await client.aclose()


@respx.mock
async def test_the_api_key_is_sent_as_x_api_key() -> None:
    route = respx.get("http://gateway:8080/v1/health").mock(return_value=httpx.Response(200))
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

    respx.get("http://gateway:8080/v1/health").mock(return_value=httpx.Response(200))
    client = _client()

    await asyncio.gather(*(client.probe() for _ in range(4)))

    inner = await client._http()
    assert inner is await client._http()
    await client.aclose()


def test_health_timeout_is_much_shorter_than_the_turn_timeout() -> None:
    """A probe that waits as long as a real request detects an outage too late."""
    settings = Settings.model_validate({"app": {"env": "test"}})

    assert settings.infra.health_timeout_s < settings.infra.timeout_s / 5


def test_a_zero_health_timeout_is_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings.model_validate({"app": {"env": "test"}, "infra": {"health_timeout_s": 0}})
