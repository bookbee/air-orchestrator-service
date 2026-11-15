"""The three operational surfaces, and the ways they must differ from each other."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx
from fastapi import FastAPI

from air_orchestrator_service.config import Settings
from tests.conftest import (
    BUSINESS_KEY,
    CLASSIFIER_BASE_URL,
    CUSTOMER_KEY,
    NO_SCOPE_KEY,
    TOOLS_GATEWAY_BASE_URL,
    auth,
    build_app,
)
from tests.unit._tools_gateway_support import (
    INVENTORY_URL,
    inventory_body,
    read_entry,
    write_entry,
)


async def test_health_needs_no_auth_and_no_dependencies(client: httpx.AsyncClient) -> None:
    """Liveness must answer with no key and with every downstream unreachable.

    This is the property that stops a dependency outage from becoming a restart
    loop, so it is asserted without any of the ``reachable_*`` fixtures on purpose.
    """
    response = await client.get("/v1/health")

    assert response.status_code == httpx.codes.OK
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "air-orchestrator-service"


@pytest.mark.usefixtures("unreachable_model_gateway", "reachable_infra")
async def test_ready_is_503_when_the_model_gateway_is_unreachable(
    client: httpx.AsyncClient,
) -> None:
    """No model gateway means no synthesis, so this replica must take itself out of rotation.

    The body is asserted alongside the status: a bare 503 sends whoever was paged to
    the logs to find out which dependency failed.
    """
    response = await client.get("/v1/ready")

    assert response.status_code == httpx.codes.SERVICE_UNAVAILABLE
    body = response.json()
    assert body["ready"] is False
    gateway = next(d for d in body["dependencies"] if d["service"] == "air-model-gateway")
    assert gateway["reachable"] is False
    assert gateway["detail"]


@pytest.mark.usefixtures("reachable_model_gateway", "reachable_infra")
async def test_ready_is_200_when_the_model_gateway_serves_us(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/ready")

    assert response.status_code == httpx.codes.OK
    assert response.json()["ready"] is True


@pytest.mark.usefixtures("reachable_model_gateway", "unreachable_infra")
async def test_ready_ignores_infra_when_the_model_gateway_serves_us(
    client: httpx.AsyncClient,
) -> None:
    """air-infra does not gate readiness: nothing in this service calls it yet.

    The behavior-changing case this migration exists for — readiness used to gate
    on air-infra alone, back when it was believed to be the model gateway. It never
    served models, so that never actually measured whether a turn could be
    answered; the model gateway does.
    """
    response = await client.get("/v1/ready")

    assert response.status_code == httpx.codes.OK
    body = response.json()
    assert body["ready"] is True
    infra = next(d for d in body["dependencies"] if d["service"] == "air-infra")
    assert infra["reachable"] is False


@pytest.mark.usefixtures("unreachable_model_gateway", "reachable_infra")
async def test_ready_is_503_even_when_infra_alone_is_reachable(client: httpx.AsyncClient) -> None:
    """air-infra being up must not paper over the model gateway being down."""
    response = await client.get("/v1/ready")

    assert response.status_code == httpx.codes.SERVICE_UNAVAILABLE
    assert response.json()["ready"] is False


@pytest.mark.usefixtures("reachable_model_gateway", "reachable_infra")
async def test_ready_ignores_optional_downstreams(client: httpx.AsyncClient) -> None:
    """Every optional service is disabled in the test settings, and readiness holds.

    This is docs/01-hld.md §7 as an executable claim: air-rag-service and
    air-tools-gateway being absent narrows what a turn can answer, and must not gate
    readiness — otherwise the service would never be ready in its own repo's
    default configuration.
    """
    response = await client.get("/v1/ready")

    assert response.json()["ready"] is True
    reported = {d["service"]: d for d in response.json()["dependencies"]}
    for service in ("air-rag", "air-tools-gateway", "air-recommender"):
        assert reported[service]["configured"] is False
        # Not probed, and the schema says so rather than implying "down".
        assert reported[service]["reachable"] is None


async def test_the_gateway_is_labelled_air_tools_gateway(client: httpx.AsyncClient) -> None:
    """REQ-TGW-023: one label for the one service, and neither retired name."""
    response = await client.get("/v1/ready")

    services = {d["service"] for d in response.json()["dependencies"]}
    assert "air-tools-gateway" in services
    assert "air-tools" not in services
    assert "air-action" not in services


def test_an_old_env_file_boots_with_one_warning_per_retired_section(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A ``.env`` from an older ``make env`` sets ``DOWNSTREAM__TOOLS__ENABLED=false``
    and ``DOWNSTREAM__ACTION__ENABLED=false``. It must still boot (spec 0012), and say
    so rather than drop the sections silently."""
    old = settings.model_copy(
        update={
            "downstream": settings.downstream.model_copy(
                update={"tools": {"enabled": "false"}, "action": {"enabled": "false"}}
            )
        }
    )

    with caplog.at_level(logging.WARNING):
        build_app(old)

    assert caplog.text.count("settings.retired_section") == 2
    assert "downstream.tools_gateway" in caplog.text


@pytest.mark.usefixtures("unreachable_model_gateway", "unreachable_infra")
async def test_ready_never_leaks_upstream_detail(client: httpx.AsyncClient) -> None:
    """The failure reason names a condition, never a URL, key or upstream body.

    /v1/ready is unauthenticated, so its body is the most exposed surface here.
    """
    response = await client.get("/v1/ready")

    reported = {d["service"]: d for d in response.json()["dependencies"]}
    unreachable = (
        ("air-model-gateway", "air-model-gateway.invalid", "8083"),
        ("air-infra", "air-infra.invalid", "8080"),
    )
    for service, host, port in unreachable:
        detail = reported[service]["detail"]
        assert "http://" not in detail
        assert host not in detail
        assert port not in detail


@pytest.mark.usefixtures("reachable_model_gateway", "reachable_infra")
async def test_ready_names_the_model_gateway(client: httpx.AsyncClient) -> None:
    """REQ-MGW-013: the dependency is reported under the gateway's own name, and the
    retired `air-llm` label appears in no response — a dashboard still filtering on
    it should find nothing rather than a stale half-match (Article VI, spec 0003)."""
    response = await client.get("/v1/ready")

    services = {d["service"] for d in response.json()["dependencies"]}
    assert "air-model-gateway" in services
    assert "air-llm" not in response.text


# ── Capabilities ──────────────────────────────────────────────────────────────


async def test_capabilities_requires_a_key(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/capabilities")

    assert response.status_code == httpx.codes.UNAUTHORIZED
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_capabilities_requires_admin_read(client: httpx.AsyncClient) -> None:
    """A valid key without the scope is 403, not 401.

    The endpoint enumerates which services a deployment talks to, which is
    reconnaissance if it leaks to any authenticated caller.
    """
    response = await client.get("/v1/capabilities", headers=auth(NO_SCOPE_KEY))

    assert response.status_code == httpx.codes.FORBIDDEN
    assert response.json()["type"].endswith("/insufficient-scope")


async def test_capabilities_reports_the_callers_own_channel(client: httpx.AsyncClient) -> None:
    """Two keys, one process, two answers.

    The channel is a property of the credential, so the same deployment describes
    itself differently depending on who asked — including a different guardrail
    profile.
    """
    customer = await client.get("/v1/capabilities", headers=auth(CUSTOMER_KEY))
    business = await client.get("/v1/capabilities", headers=auth(BUSINESS_KEY))

    assert customer.json()["channel"] == "customer"
    assert business.json()["channel"] == "business"

    # Schema validation is the business channel's contract and meaningless on the
    # customer one, which returns prose.
    assert customer.json()["guardrails"]["validate_schema"] is False
    assert business.json()["guardrails"]["validate_schema"] is True


async def test_capabilities_reports_only_configured_routes(client: httpx.AsyncClient) -> None:
    """With every downstream off, `direct` is the only route.

    The model gateway alone can answer directly, which is why it is unconditional while the
    rest appear as their services are enabled.
    """
    response = await client.get("/v1/capabilities", headers=auth(CUSTOMER_KEY))

    assert response.json()["routes"] == ["direct"]


async def test_capabilities_reports_token_deltas(client: httpx.AsyncClient) -> None:
    """REQ-STRM-011: this build streams the answer, and says so.

    A client must be able to tell a build that *cannot* stream tokens from one that
    merely did not this time, so the capability is reported rather than implied.
    """
    response = await client.get("/v1/capabilities", headers=auth(CUSTOMER_KEY))

    streaming = response.json()["streaming"]
    assert streaming["sse"] is True
    assert streaming["stage_events"] is True
    assert streaming["token_deltas"] is True


# ── Metrics ───────────────────────────────────────────────────────────────────


async def test_metrics_exposes_the_prometheus_payload(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")

    assert response.status_code == httpx.codes.OK
    assert response.headers["content-type"].startswith("text/plain")
    # Registered eagerly at import, so the family is present before any turn has run.
    assert "air_orchestrator_service_requests_total" in response.text


async def test_metrics_is_outside_the_version_prefix(client: httpx.AsyncClient) -> None:
    """A metric name is not part of the API contract, so it must not sit under /v1.

    Asserted by request rather than by reading ``app.routes``: FastAPI 0.141 resolves
    an included router lazily, so that collection is still empty here and would make
    this test pass for the wrong reason — or fail for one, as it first did.
    """
    assert (await client.get("/metrics")).status_code == httpx.codes.OK
    assert (await client.get("/v1/metrics")).status_code == httpx.codes.NOT_FOUND


async def test_metrics_is_absent_from_the_openapi_document(app: FastAPI) -> None:
    """Excluded from the schema so a generated client does not offer it as an operation."""
    assert "/metrics" not in app.openapi()["paths"]


@pytest.mark.usefixtures("reachable_model_gateway", "reachable_infra")
async def test_capabilities_report_live_downstream_status(settings: Settings) -> None:
    """REQ-CLB-007 / spec 0010 AC-2: air-rag-service configured but down drops the `rag` route;
    air-classifier up is reported reachable."""
    downstream = settings.downstream.model_copy(
        update={
            "rag": settings.downstream.rag.model_copy(
                update={"enabled": True, "base_url": "http://air-rag-service.invalid:8087"}
            ),
            "classifier": settings.downstream.classifier.model_copy(
                update={"enabled": True, "base_url": CLASSIFIER_BASE_URL}
            ),
        }
    )
    app = build_app(settings.model_copy(update={"downstream": downstream}))
    transport = httpx.ASGITransport(app=app)
    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://air-rag-service.invalid:8087/v1/health").mock(
            side_effect=httpx.ConnectError("refused")
        )
        mock.get(f"{CLASSIFIER_BASE_URL}/v1/health").mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/capabilities", headers=auth(CUSTOMER_KEY))

    body = response.json()
    reported = {d["service"]: d for d in body["downstreams"]}
    assert reported["air-rag"]["reachable"] is False
    assert reported["air-rag"]["detail"] == "connection failed (ConnectError)"
    assert reported["air-classifier"]["reachable"] is True
    assert body["routes"] == ["direct"]

    with respx.mock(assert_all_called=False) as mock:
        mock.get("http://air-rag-service.invalid:8087/v1/health").mock(
            return_value=httpx.Response(200)
        )
        mock.get(f"{CLASSIFIER_BASE_URL}/v1/health").mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/capabilities", headers=auth(CUSTOMER_KEY))
    assert response.json()["routes"] == ["direct", "rag"]


async def test_ready_does_not_probe_optional_downstreams(settings: Settings) -> None:
    """REQ-CLB-008: configured optional services are reported, never called."""
    from air_orchestrator_service.api.deps import STATE_ATTR
    from air_orchestrator_service.constants import DownstreamService
    from air_orchestrator_service.schemas.common import DependencyStatus

    downstream = settings.downstream.model_copy(
        update={
            "rag": settings.downstream.rag.model_copy(
                update={"enabled": True, "base_url": "http://air-rag-service.invalid:8087"}
            )
        }
    )
    app = build_app(settings.model_copy(update={"downstream": downstream}))
    state = getattr(app.state, STATE_ATTR)
    for client_, service in (
        (state.model_gateway, DownstreamService.MODEL_GATEWAY),
        (state.infra, DownstreamService.INFRA),
    ):

        async def healthy(service: DownstreamService = service) -> DependencyStatus:
            return DependencyStatus(service=service, configured=True, reachable=True)

        client_.probe = healthy
    transport = httpx.ASGITransport(app=app)
    with respx.mock(assert_all_called=False) as mock:
        health = mock.get("http://air-rag-service.invalid:8087/v1/health").mock(
            return_value=httpx.Response(200)
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/ready")

    assert health.call_count == 0
    rag = next(d for d in response.json()["dependencies"] if d["service"] == "air-rag")
    assert rag["configured"] is True
    assert rag["reachable"] is None


_GATEWAY_ENTRIES: dict[str, list[dict[str, object]]] = {
    "reads only": [read_entry("orders.list_recent")],
    "writes only": [write_entry("orders.cancel")],
    "nothing": [],
    "both": [read_entry("orders.list_recent"), write_entry("orders.cancel")],
}


@pytest.mark.parametrize(
    ("key", "entries", "reachable", "expected"),
    [
        (CUSTOMER_KEY, "reads only", True, ["direct", "tools"]),
        (CUSTOMER_KEY, "writes only", True, ["action", "direct"]),
        (CUSTOMER_KEY, "nothing", True, ["direct"]),
        (CUSTOMER_KEY, "both", True, ["action", "direct", "tools"]),
        (BUSINESS_KEY, "both", True, ["direct", "tools"]),
        (CUSTOMER_KEY, "both", False, ["direct"]),
    ],
    ids=["reads", "writes", "nothing", "both", "business", "unreachable"],
)
async def test_capabilities_list_tools_and_action_only_when_the_gateway_offers_them(
    settings: Settings, key: str, entries: str, reachable: bool, expected: list[str]
) -> None:
    """``REQ-TGW-005``. ``tools`` needs a ``read`` entry and ``action`` a ``write`` one,
    in the caller's tenant inventory narrowed to its channel. The business channel
    never lists ``action`` (``REQ-TGW-026``), and a gateway that does not answer its
    health check lists neither and is not asked for an inventory. Every entry needs a
    customer, and the admin key names none: that does not hide them."""
    gateway = settings.downstream.tools_gateway.model_validate(
        {
            "enabled": True,
            "base_url": TOOLS_GATEWAY_BASE_URL,
            "tenant_keys": {
                "tenant-a": {"read": "airtg_read_a", "execute": "airtg_exec_a"},
                "tenant-b": {"read": "airtg_read_b", "execute": "airtg_exec_b"},
            },
            "allow": {"customer": ["orders.*"], "business": ["orders.*"]},
        }
    )
    downstream = settings.downstream.model_copy(update={"tools_gateway": gateway})
    app = build_app(settings.model_copy(update={"downstream": downstream}))
    transport = httpx.ASGITransport(app=app)
    with respx.mock(assert_all_called=False) as mock:
        health = mock.get(f"{TOOLS_GATEWAY_BASE_URL}/v1/health")
        if reachable:
            health.mock(return_value=httpx.Response(200))
        else:
            health.mock(side_effect=httpx.ConnectError("refused"))
        inventory = mock.get(INVENTORY_URL).mock(
            return_value=httpx.Response(200, json=inventory_body(*_GATEWAY_ENTRIES[entries]))
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/capabilities", headers=auth(key))

    assert response.status_code == httpx.codes.OK, response.text
    assert response.json()["routes"] == expected
    assert inventory.call_count == (1 if reachable else 0)
