"""Shared fixtures.

Every test builds its app through :func:`air_platform.main.create_app` with explicit
settings rather than importing ``main.app``. That keeps a test from depending on the
ambient environment, and keeps two tests from sharing one app's state.

No test in this suite touches the network. The air-infra probe is either stubbed or
allowed to fail — a readiness check that reports "unreachable" is a perfectly good
result to assert on, and is what a fresh checkout genuinely produces.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from air_platform.config import Settings
from air_platform.constants import (
    SCOPE_ADMIN_READ,
    SCOPE_CHAT_WRITE,
    SCOPE_QUERY_WRITE,
    SCOPE_SESSION_READ,
    DownstreamService,
)
from air_platform.main import create_app
from air_platform.schemas.common import DependencyStatus

TEST_SALT = "test-salt"
CUSTOMER_KEY = "airp_test_customer"
BUSINESS_KEY = "airp_test_business"
NO_SCOPE_KEY = "airp_test_noscope"


def digest(raw: str, salt: str = TEST_SALT) -> str:
    return hashlib.sha256((salt + raw).encode("utf-8")).hexdigest()


def key_records() -> list[dict[str, Any]]:
    """Three keys covering the axes the guards discriminate on: channel and scope."""
    return [
        {
            "id": "customer",
            "name": "Customer test key",
            "key_hash": digest(CUSTOMER_KEY),
            "channel": "customer",
            "tenant": "tenant-a",
            "scopes": [SCOPE_CHAT_WRITE, SCOPE_SESSION_READ, SCOPE_ADMIN_READ],
        },
        {
            "id": "business",
            "name": "Business test key",
            "key_hash": digest(BUSINESS_KEY),
            "channel": "business",
            "tenant": "tenant-b",
            "scopes": [SCOPE_QUERY_WRITE, SCOPE_SESSION_READ, SCOPE_ADMIN_READ],
        },
        {
            "id": "noscope",
            "name": "Scopeless test key",
            "key_hash": digest(NO_SCOPE_KEY),
            "channel": "customer",
            "tenant": "tenant-a",
            "scopes": [],
        },
    ]


@pytest.fixture
def settings() -> Settings:
    """Test settings. ``env=test`` so the production guards stay off the path."""
    return Settings.model_validate(
        {
            "app": {"env": "test", "port": 8081},
            "security": {
                "hash_salt": TEST_SALT,
                "api_keys_inline": json.dumps(key_records()),
                "allow_unauthenticated": False,
            },
            "obs": {"log_format": "console", "metrics_enabled": True},
        }
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """In-process client. ASGITransport means no socket is opened.

    ``LifespanManager`` is not used: the lifespan here only logs and closes the
    air-infra pool, and driving it would add an async dependency for no coverage.
    Tests that need the pool closed do it explicitly.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
def reachable_infra(app: FastAPI) -> Iterator[None]:
    """Make the air-infra probe report healthy.

    Patches the client rather than intercepting HTTP: the probe's own error handling
    is tested directly in ``test_infra_client.py``, and here we only want readiness
    to see a healthy dependency.
    """
    from air_platform.api.deps import STATE_ATTR

    state = getattr(app.state, STATE_ATTR)

    async def healthy() -> DependencyStatus:
        return DependencyStatus(
            service=DownstreamService.INFRA,
            configured=True,
            reachable=True,
            latency_ms=1.0,
        )

    original = state.infra.probe
    state.infra.probe = healthy  # type: ignore[method-assign]
    try:
        yield
    finally:
        state.infra.probe = original  # type: ignore[method-assign]


def auth(key: str) -> dict[str, str]:
    return {"X-API-Key": key}
