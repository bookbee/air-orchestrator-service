"""Shared fixtures.

Every test builds its app through :func:`air_orchestrator_service.main.create_app` with explicit
settings rather than importing ``main.app``. That keeps a test from depending on the
ambient environment, and keeps two tests from sharing one app's state.

**No test may depend on what is running on the developer's machine.** Both the
air-infra and air-model-gateway probes are always stubbed, in both directions:
``reachable_infra``/``unreachable_infra`` and
``reachable_model_gateway``/``unreachable_model_gateway``.
An earlier version of this file let the "unreachable" case simply *happen*, on the
reasoning that a fresh checkout has no gateway running — and those tests duly broke
the first time air-infra was started locally. A test whose result depends on the
ambient environment is not testing what it claims to.

``INFRA_BASE_URL`` and ``MODEL_GATEWAY_BASE_URL`` are deliberately not localhost, so that a
probe escaping the stubs fails loudly rather than quietly reaching a real service.

**Nor on the developer's ``.env``.** ``Settings`` is a pydantic-settings model, so even
``Settings.model_validate({...})`` merges in ``.env`` and ``AIR_ORCHESTRATOR_SERVICE__*``
variables. ``_no_ambient_settings`` switches both off for every test. Without it, a
``.env`` written by ``make env`` to drive the service by hand (classifier enabled, real
keys) silently enabled downstreams in tests, which then called the live services on
localhost.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import (
    SCOPE_ADMIN_READ,
    SCOPE_CHAT_WRITE,
    SCOPE_QUERY_WRITE,
    SCOPE_SESSION_READ,
    DownstreamService,
)
from air_orchestrator_service.engine.plan import Plan, PlanContext, Planner
from air_orchestrator_service.main import create_app
from air_orchestrator_service.schemas.common import DependencyStatus

TEST_SALT = "test-salt"
#: Read from the model rather than restated, so a renamed prefix cannot leak through.
ENV_PREFIX = str(Settings.model_config["env_prefix"]).upper()
#: Not localhost: a probe that escapes the stubs must fail, not reach a real gateway.
INFRA_BASE_URL = "http://air-infra.invalid:8080"
#: Not localhost, for the same reason: escaping the stub must fail loudly.
MODEL_GATEWAY_BASE_URL = "http://air-model-gateway.invalid:8083"
#: Not localhost, for the same reason. Off by default, as in production.
CLASSIFIER_BASE_URL = "http://air-classifier.invalid:8082"
#: air-tools-gateway (spec 0012). ``.invalid`` like the rest: an escaping call fails loudly.
TOOLS_GATEWAY_BASE_URL = "http://air-tools-gateway.invalid:8084"
CUSTOMER_KEY = "airo_test_customer"
BUSINESS_KEY = "airo_test_business"
NO_SCOPE_KEY = "airo_test_noscope"


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


@pytest.fixture(autouse=True)
def _no_ambient_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every ``Settings`` a test builds sees only what the test passes it."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in list(os.environ):
        if name.upper().startswith(ENV_PREFIX):
            monkeypatch.delenv(name)


@pytest.fixture
def settings() -> Settings:
    """Test settings. ``env=test`` so the production guards stay off the path."""
    return Settings.model_validate(
        {
            "app": {"env": "test", "port": 8081},
            "infra": {"base_url": INFRA_BASE_URL},
            "model_gateway": {"base_url": MODEL_GATEWAY_BASE_URL},
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
    ``ModelGatewayClient.generate`` to get a 200 at all — patched here, once,
    rather than as a fixture every turn-test file has to remember to request,
    the same reasoning that put ``reachable_infra``/``reachable_model_gateway``
    one level up from the tests that need them. A test that wants the real
    failure path reassigns ``state.model_gateway.generate`` again itself. An SSE
    turn streams (spec 0004), so ``stream`` is scripted the same way — the
    canned answer in two deltas — and a streaming test passes its own script.
    """
    return build_app(settings)


def build_app(settings: Settings) -> FastAPI:
    """What the ``app`` fixture builds, for a test that needs bespoke settings."""
    from air_orchestrator_service.api.deps import STATE_ATTR

    built = create_app(settings)
    state = getattr(built.state, STATE_ATTR)
    state.model_gateway.generate = _canned_generate  # type: ignore[method-assign]
    state.model_gateway.stream = scripted_stream(  # type: ignore[method-assign]
        CANNED_ANSWER[:20], CANNED_ANSWER[20:], canned_summary()
    )
    return built


class FixedPlanner:
    """Plans the same thing for every turn: spec 0012's "injected plan" (§3).

    Records each context it was asked about, so a test can check what the engine
    let a planner see.
    """

    def __init__(self, plan: Plan) -> None:
        self._plan = plan
        self.seen: list[PlanContext] = []

    async def plan(self, ctx: PlanContext) -> Plan:
        self.seen.append(ctx)
        return self._plan


def use_planner(app: FastAPI, planner: Planner) -> None:
    """Swap the planner on an otherwise real app. ``AppState`` is frozen, so it is
    replaced whole, collaborators and all."""
    from dataclasses import replace

    from air_orchestrator_service.api.deps import STATE_ATTR

    setattr(app.state, STATE_ATTR, replace(getattr(app.state, STATE_ATTR), planner=planner))


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """In-process client. ASGITransport means no socket is opened.

    ``LifespanManager`` is not used: the lifespan here only logs and closes the
    air-infra and air-model-gateway pools, and driving it would add an async dependency for
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
    from air_orchestrator_service.api.deps import STATE_ATTR

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
def reachable_model_gateway(app: FastAPI) -> Iterator[None]:
    """Make the air-model-gateway probe report that it serves this service.

    Patches the client rather than intercepting HTTP: the probe's own error handling
    is tested directly in ``test_model_gateway_client.py``, and here we only want
    readiness to see a healthy dependency.
    """
    from air_orchestrator_service.api.deps import STATE_ATTR

    state = getattr(app.state, STATE_ATTR)

    async def healthy() -> DependencyStatus:
        return DependencyStatus(
            service=DownstreamService.MODEL_GATEWAY,
            configured=True,
            reachable=True,
            latency_ms=1.0,
        )

    original = state.model_gateway.probe
    state.model_gateway.probe = healthy  # type: ignore[method-assign]
    try:
        yield
    finally:
        state.model_gateway.probe = original  # type: ignore[method-assign]


@pytest.fixture
def unreachable_model_gateway() -> Iterator[None]:
    """Make the air-model-gateway probe fail, deterministically.

    Mocks the transport rather than patching :meth:`ModelGatewayClient.probe`, so the
    real error handling runs — the reason a route-level test can still assert that no
    URL leaks into the readiness body. Both probe routes fail, as they would when
    the gateway is down.
    """
    with respx.mock:
        for path in ("/v1/ready", "/v1/capabilities"):
            respx.get(f"{MODEL_GATEWAY_BASE_URL}{path}").mock(
                side_effect=httpx.ConnectError("connection refused")
            )
        yield


#: The turn engine's default answer, whenever a test does not override the
#: synthesis mock itself. Tests that care what the text says (the mutation
#: gate, the proposal flow) assert on their own canned strings from
#: `TurnEngine._answer` — none of those ever reach the model — so this value
#: only has to be stable, not meaningful.
CANNED_ANSWER = "This is a mocked answer from air-model-gateway."


async def _canned_generate(
    *,
    messages: list[dict[str, str]],
    max_tokens: int = 1024,
    json_schema: dict[str, Any] | None = None,
    schema_name: str | None = None,
    timeout: float | None = None,  # noqa: ASYNC109 — mirrors ModelGatewayClient.generate
) -> Any:
    """A deterministic stand-in for ``ModelGatewayClient.generate`` — see the
    ``app`` fixture. A schema-constrained call (``json_schema`` set) gets one
    placeholder value per declared property rather than the plain canned
    string, so ``TurnEngine._structured_answer`` has real JSON to parse.
    """
    from air_orchestrator_service.clients.model_gateway import GenerateResult, GenerateUsage

    if json_schema is not None:
        properties = json_schema.get("properties") or {}
        content = json.dumps(dict.fromkeys(properties, "mock"))
    else:
        content = CANNED_ANSWER
    return GenerateResult(
        content=content,
        cost_usd=0.001,
        finish_reason="stop",
        usage=GenerateUsage(prompt_tokens=10, completion_tokens=5),
    )


def auth(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


#: In a :func:`scripted_stream` script, the stream stops producing and waits forever.
HANG = object()


def canned_summary(*, refusal: bool = False, cost_usd: float = 0.001) -> Any:
    """The gateway's ``done`` summary for a scripted stream: no content (that was
    the deltas), usage and cost as a real stream reports them."""
    from air_orchestrator_service.clients.model_gateway import GenerateResult, GenerateUsage

    return GenerateResult(
        content="",
        refusal=refusal,
        cost_usd=cost_usd,
        finish_reason="stop",
        usage=GenerateUsage(prompt_tokens=10, completion_tokens=5),
    )


class ScriptedStream:
    """A stand-in for ``ModelGatewayClient.stream`` that replays a script through a
    *real* :class:`GatewayStream` — so the queue, the per-read timeout and close-on-
    exit are the production ones, and only the HTTP is replaced.

    Script items: ``str`` → a delta; a ``GenerateResult`` → ``done``; a
    ``ModelGatewayError`` → raised by the read that reaches it; :data:`HANG` → the
    producer stalls until cancelled. ``calls`` counts opened streams; ``closed``
    records that the producer was cancelled rather than running to its end.
    """

    def __init__(self, *script: Any) -> None:
        self.script = script
        self.calls = 0
        self.closed = False

    def __call__(self, *, messages: list[dict[str, str]], max_tokens: int = 1024) -> Any:
        import asyncio

        from air_orchestrator_service.clients.model_gateway import (
            GatewayStream,
            GenerateResult,
            ModelGatewayError,
            StreamDelta,
            StreamDone,
        )

        self.calls += 1

        async def produce(put: Any) -> None:
            try:
                for item in self.script:
                    await asyncio.sleep(0)
                    if item is HANG:
                        await asyncio.Event().wait()
                    elif isinstance(item, str):
                        put(StreamDelta(item))
                    elif isinstance(item, GenerateResult):
                        put(StreamDone(item))
                    elif isinstance(item, ModelGatewayError):
                        put(item)
                        return
                    else:
                        raise TypeError(f"unscriptable stream item: {item!r}")
            except asyncio.CancelledError:
                self.closed = True
                raise

        return GatewayStream(produce)


def scripted_stream(*script: Any) -> ScriptedStream:
    """Build a :class:`ScriptedStream` — see its docstring for the script items."""
    return ScriptedStream(*script)
