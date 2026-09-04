"""Shared fixtures.

Every test builds its app through :func:`air_platform.main.create_app` with explicit
settings rather than importing ``main.app``. That keeps a test from depending on the
ambient environment, and keeps two tests from sharing one app's state.

**No test may depend on what is running on the developer's machine.** Both the
air-infra and air-llm probes are always stubbed, in both directions:
``reachable_infra``/``unreachable_infra`` and ``reachable_llm``/``unreachable_llm``.
An earlier version of this file let the "unreachable" case simply *happen*, on the
reasoning that a fresh checkout has no gateway running — and those tests duly broke
the first time air-infra was started locally. A test whose result depends on the
ambient environment is not testing what it claims to.

``INFRA_BASE_URL`` and ``LLM_BASE_URL`` are deliberately not localhost, so that a
probe escaping the stubs fails loudly rather than quietly reaching a real service.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
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
#: Not localhost: a probe that escapes the stubs must fail, not reach a real gateway.
INFRA_BASE_URL = "http://air-infra.invalid:8080"
#: Not localhost, for the same reason: escaping the stub must fail loudly.
LLM_BASE_URL = "http://air-llm.invalid:8083"
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
            "infra": {"base_url": INFRA_BASE_URL},
            "llm": {"base_url": LLM_BASE_URL},
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
    """The app, with a deterministic synthesis call already patched in.

    Every route test that reaches ``TurnEngine`` needs a working
    ``LlmClient.chat`` to get a 200 at all — patched here, once, rather than
    as a fixture every turn-test file has to remember to request, the same
    reasoning that put ``reachable_infra``/``reachable_llm`` one level up
    from the tests that need them. A test that wants the real failure path
    reassigns ``state.llm.chat`` again itself.
    """
    from air_platform.api.deps import STATE_ATTR

    built = create_app(settings)
    state = getattr(built.state, STATE_ATTR)
    state.llm.chat = _canned_chat  # type: ignore[method-assign]
    return built


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """In-process client. ASGITransport means no socket is opened.

    ``LifespanManager`` is not used: the lifespan here only logs and closes the
    air-infra and air-llm pools, and driving it would add an async dependency for
    no coverage. Tests that need a pool closed do it explicitly.
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


@pytest.fixture
def unreachable_infra() -> Iterator[None]:
    """Make the air-infra probe fail, deterministically.

    Mocks the transport rather than patching :meth:`InfraClient.probe`, so the real
    error handling runs — the reason a route-level test can still assert that no URL
    leaks into the readiness body.
    """
    with respx.mock:
        respx.get(f"{INFRA_BASE_URL}/v1/health").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        yield


@pytest.fixture
def reachable_llm(app: FastAPI) -> Iterator[None]:
    """Make the air-llm probe report healthy.

    Patches the client rather than intercepting HTTP: the probe's own error handling
    is tested directly in ``test_llm_client.py``, and here we only want readiness
    to see a healthy dependency.
    """
    from air_platform.api.deps import STATE_ATTR

    state = getattr(app.state, STATE_ATTR)

    async def healthy() -> DependencyStatus:
        return DependencyStatus(
            service=DownstreamService.LLM,
            configured=True,
            reachable=True,
            latency_ms=1.0,
        )

    original = state.llm.probe
    state.llm.probe = healthy  # type: ignore[method-assign]
    try:
        yield
    finally:
        state.llm.probe = original  # type: ignore[method-assign]


@pytest.fixture
def unreachable_llm() -> Iterator[None]:
    """Make the air-llm probe fail, deterministically.

    Mocks the transport rather than patching :meth:`LlmClient.probe`, so the real
    error handling runs — the reason a route-level test can still assert that no URL
    leaks into the readiness body.
    """
    with respx.mock:
        respx.get(f"{LLM_BASE_URL}/v1/ready").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        yield


#: The turn engine's default answer, whenever a test does not override the
#: synthesis mock itself. Tests that care what the text says (the mutation
#: gate, the proposal flow) assert on their own canned strings from
#: `TurnEngine._answer` — none of those ever reach the model — so this value
#: only has to be stable, not meaningful.
CANNED_ANSWER = "This is a mocked answer from air-llm."


async def _canned_chat(
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 1024,
    json_schema: dict[str, Any] | None = None,
    schema_name: str | None = None,
    timeout: float | None = None,
) -> Any:
    """A deterministic stand-in for ``LlmClient.chat`` — see the ``app``
    fixture. A schema-constrained call (``json_schema`` set) gets one
    placeholder value per declared property rather than the plain canned
    string, so ``TurnEngine._structured_answer`` has real JSON to parse.
    """
    from air_platform.clients.llm import ChatResult, ChatUsage

    if json_schema is not None:
        properties = json_schema.get("properties") or {}
        content = json.dumps(dict.fromkeys(properties, "mock"))
    else:
        content = CANNED_ANSWER
    return ChatResult(
        content=content, cost_usd=0.001, usage=ChatUsage(prompt_tokens=10, completion_tokens=5)
    )


def auth(key: str) -> dict[str, str]:
    return {"X-API-Key": key}
