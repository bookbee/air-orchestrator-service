"""What the service promises whatever stands in front of it (spec 0016).

A front door (a load balancer, a gateway) forwards some routes, adds headers of its
own and passes on whatever headers a client sent. These tests pin the three properties
that make that safe: every route is knowingly public or internal, nothing a proxy can
set says who the caller is, and no browser is invited.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from tests.conftest import CUSTOMER_KEY, auth

#: What a front door forwards (``REQ-FDR-001``). air-infrastructure-service's
#: ``gke/values/*/air-orchestrator-service.yaml`` lists the same paths; a change here
#: has a second half there.
PUBLIC_ROUTES = {
    ("POST", "/v1/chat"),
    ("POST", "/v1/query"),
    ("GET", "/v1/sessions/{session_id}"),
    ("DELETE", "/v1/sessions/{session_id}"),
}

#: What stays inside the cluster: probes, the operator's view and the scrape target.
INTERNAL_ROUTES = {
    ("GET", "/v1/health"),
    ("GET", "/v1/ready"),
    ("GET", "/v1/capabilities"),
    ("GET", "/metrics"),
}

#: Headers a proxy sets and a client can forge.
PROXY_HEADERS = {
    "X-Forwarded-For": "203.0.113.9, 10.0.0.1",
    "X-Forwarded-Host": "othershop.example",
    "X-Forwarded-Proto": "https",
    "Forwarded": "for=203.0.113.9;host=othershop.example;proto=https",
    "X-Real-IP": "203.0.113.9",
    "Via": "1.1 forged-proxy",
}


def test_every_route_is_declared_public_or_internal(app: FastAPI) -> None:
    """``REQ-FDR-001``: a new route must be put in one set on purpose.

    The front door forwards the public set only. Without this, a route added later
    would be reachable or unreachable from outside by accident, and nobody would have
    decided which.

    The routes are read from the OpenAPI document, the one stable listing FastAPI
    gives. ``/metrics`` is the single route kept out of that document on purpose
    (``test_metrics_is_absent_from_the_openapi_document``), so it is declared internal
    here and left out of the comparison.
    """
    documented = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }

    assert documented == PUBLIC_ROUTES | (INTERNAL_ROUTES - {("GET", "/metrics")})
    assert not PUBLIC_ROUTES & INTERNAL_ROUTES


async def test_proxy_headers_never_change_who_the_caller_is(client: httpx.AsyncClient) -> None:
    """``REQ-FDR-003``: identity is the credential's, never a header a proxy may set.

    A client that could name its own origin, host or address through a forwarded
    header and be believed could choose its tenant or its channel, which is what
    Article I exists to prevent.
    """
    body = {"message": "hello"}

    plain = await client.post("/v1/chat", json=body, headers=auth(CUSTOMER_KEY))
    forwarded = await client.post(
        "/v1/chat", json=body, headers={**auth(CUSTOMER_KEY), **PROXY_HEADERS}
    )
    anonymous = await client.post("/v1/chat", json=body, headers=PROXY_HEADERS)
    wrong_channel = await client.post(
        "/v1/query", json={"query": "hello"}, headers={**auth(CUSTOMER_KEY), **PROXY_HEADERS}
    )

    assert plain.status_code == httpx.codes.OK
    assert forwarded.status_code == httpx.codes.OK
    assert forwarded.json()["status"] == plain.json()["status"]
    # No header stands in for a key, and none moves a key to the other channel.
    assert anonymous.status_code == httpx.codes.UNAUTHORIZED
    assert wrong_channel.status_code == httpx.codes.FORBIDDEN


async def test_a_cross_origin_preflight_is_not_answered(client: httpx.AsyncClient) -> None:
    """``REQ-FDR-009``: browsers are not clients, so nothing invites one.

    The API key must never reach a browser. A CORS middleware added for convenience
    would make that mistake look supported.
    """
    response = await client.options(
        "/v1/chat",
        headers={
            "Origin": "https://shop.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-api-key, content-type",
        },
    )

    assert not response.is_success
    assert not any(name.lower().startswith("access-control-") for name in response.headers)
