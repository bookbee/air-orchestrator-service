"""The three operational surfaces, and the ways they must differ from each other."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from tests.conftest import BUSINESS_KEY, CUSTOMER_KEY, NO_SCOPE_KEY, auth


async def test_health_needs_no_auth_and_no_dependencies(client: httpx.AsyncClient) -> None:
    """Liveness must answer with no key and with air-infra unreachable.

    This is the property that stops a dependency outage from becoming a restart
    loop, so it is asserted without the ``reachable_infra`` fixture on purpose.
    """
    response = await client.get("/v1/health")

    assert response.status_code == httpx.codes.OK
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "air-platform"


async def test_ready_is_503_when_infra_is_unreachable(client: httpx.AsyncClient) -> None:
    """No gateway means no synthesis, so this replica must take itself out of rotation.

    The body is asserted alongside the status: a bare 503 sends whoever was paged to
    the logs to find out which dependency failed.
    """
    response = await client.get("/v1/ready")

    assert response.status_code == httpx.codes.SERVICE_UNAVAILABLE
    body = response.json()
    assert body["ready"] is False
    infra = next(d for d in body["dependencies"] if d["service"] == "air-infra")
    assert infra["reachable"] is False
    assert infra["detail"]


@pytest.mark.usefixtures("reachable_infra")
async def test_ready_is_200_when_infra_is_reachable(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/ready")

    assert response.status_code == httpx.codes.OK
    assert response.json()["ready"] is True


@pytest.mark.usefixtures("reachable_infra")
async def test_ready_ignores_optional_downstreams(client: httpx.AsyncClient) -> None:
    """Every optional service is disabled in the test settings, and readiness holds.

    This is docs/01-hld.md §7 as an executable claim: air-rag, air-tools and
    air-action being absent narrows what a turn can answer, and must not gate
    readiness — otherwise the service would never be ready in its own repo's
    default configuration.
    """
    response = await client.get("/v1/ready")

    assert response.json()["ready"] is True
    reported = {d["service"]: d for d in response.json()["dependencies"]}
    for service in ("air-rag", "air-tools", "air-action", "air-recommender"):
        assert reported[service]["configured"] is False
        # Not probed, and the schema says so rather than implying "down".
        assert reported[service]["reachable"] is None


async def test_ready_never_leaks_upstream_detail(client: httpx.AsyncClient) -> None:
    """The failure reason names a condition, never a URL, key or upstream body.

    /v1/ready is unauthenticated, so its body is the most exposed surface here.
    """
    response = await client.get("/v1/ready")

    detail = next(
        d["detail"] for d in response.json()["dependencies"] if d["service"] == "air-infra"
    )
    assert "http://" not in detail
    assert "localhost" not in detail
    assert "8080" not in detail


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

    air-infra alone can answer directly, which is why it is unconditional while the
    rest appear as their services are enabled.
    """
    response = await client.get("/v1/capabilities", headers=auth(CUSTOMER_KEY))

    assert response.json()["routes"] == ["direct"]


async def test_capabilities_admits_it_cannot_stream_tokens(client: httpx.AsyncClient) -> None:
    """The recorded deviation from the diagram, reported rather than implied.

    docs/01-hld.md §5: v1 streams stage events because air-infra's gateway returns
    complete responses. A client must be able to tell a build that *cannot* stream
    tokens from one that merely did not this time.
    """
    response = await client.get("/v1/capabilities", headers=auth(CUSTOMER_KEY))

    streaming = response.json()["streaming"]
    assert streaming["sse"] is True
    assert streaming["stage_events"] is True
    assert streaming["token_deltas"] is False


# ── Metrics ───────────────────────────────────────────────────────────────────


async def test_metrics_exposes_the_prometheus_payload(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")

    assert response.status_code == httpx.codes.OK
    assert response.headers["content-type"].startswith("text/plain")
    # Registered eagerly at import, so the family is present before any turn has run.
    assert "air_platform_requests_total" in response.text


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
