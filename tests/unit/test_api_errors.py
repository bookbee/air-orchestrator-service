"""One error shape, and nothing internal in it."""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from air_platform.api.errors import (
    ChannelMismatchError,
    DependencyUnavailableError,
    InsufficientScopeError,
    ProposalNotFoundError,
    SessionNotFoundError,
    TurnBudgetExceededError,
    _slug_for_status,
)
from tests.conftest import CUSTOMER_KEY, auth


async def test_unknown_route_is_a_problem_document(client: httpx.AsyncClient) -> None:
    """Starlette's own 404 must not escape as FastAPI's `{"detail": ...}`.

    A caller writes one parser, so the framework's default shape is as much a bug as
    a wrong status would be.
    """
    response = await client.get("/v1/does-not-exist")

    assert response.status_code == httpx.codes.NOT_FOUND
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"].endswith("/not-found")
    assert body["status"] == httpx.codes.NOT_FOUND
    assert body["instance"] == "/v1/does-not-exist"


async def test_method_mismatch_keeps_the_problem_shape(client: httpx.AsyncClient) -> None:
    """405 is not in the §12 catalogue, so its slug is derived from the HTTP phrase."""
    response = await client.post("/v1/health")

    assert response.status_code == httpx.codes.METHOD_NOT_ALLOWED
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"].endswith("/method-not-allowed")


async def test_every_error_carries_the_request_id_in_body_and_header(
    client: httpx.AsyncClient,
) -> None:
    """Correlation must work for a client that only reads headers on failures."""
    response = await client.get("/v1/capabilities")

    assert response.status_code == httpx.codes.UNAUTHORIZED
    assert response.json()["request_id"] == response.headers["X-Request-ID"]


async def test_a_supplied_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/health", headers={"X-Request-ID": "caller-supplied-1"})

    assert response.headers["X-Request-ID"] == "caller-supplied-1"


async def test_a_hostile_request_id_is_replaced_not_sanitised(client: httpx.AsyncClient) -> None:
    """Trimming a hostile id to its safe characters yields something that still looks
    like the caller's id — which is the confusion an attacker wants when the two
    appear side by side in a log. A fresh id is unambiguous."""
    response = await client.get("/v1/health", headers={"X-Request-ID": "bad\nid: forged"})

    echoed = response.headers["X-Request-ID"]
    assert echoed.startswith("req_")
    assert "forged" not in echoed


async def test_the_body_ceiling_is_a_400_malformed_request(app: FastAPI) -> None:
    """§12 files an oversized payload under `malformed-request`, not 413.

    The ceiling is a parsing limit rather than a quota, and the catalogue is the
    contract the client codes against.
    """
    transport = httpx.ASGITransport(app=app)
    oversized = b"x" * (app.state.air_state.settings.app.max_body_bytes + 1)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            "/v1/capabilities", content=oversized, headers=auth(CUSTOMER_KEY)
        )

    assert response.status_code == httpx.codes.BAD_REQUEST
    assert response.json()["type"].endswith("/malformed-request")


def test_the_catalogue_gives_each_failure_a_distinct_slug() -> None:
    """A client routes on `type`, so two different failures must not share one."""
    slugs = {
        cls.slug
        for cls in (
            InsufficientScopeError,
            ChannelMismatchError,
            SessionNotFoundError,
            ProposalNotFoundError,
            TurnBudgetExceededError,
            DependencyUnavailableError,
        )
    }

    assert len(slugs) == 6


def test_channel_mismatch_is_distinguishable_from_a_missing_scope() -> None:
    """Both are 403, and they send an integrator to different places.

    "Insufficient scope" would have them auditing scopes when the real problem is
    which key they used.
    """
    assert ChannelMismatchError.status == InsufficientScopeError.status == 403
    assert ChannelMismatchError.slug != InsufficientScopeError.slug


def test_generic_403_and_404_resolve_to_the_generic_catalogue_entries() -> None:
    """A framework-raised status must not land on one of the narrower entries.

    `_BY_STATUS` is built from a fixed order for exactly this reason: a Starlette
    404 is `not-found`, never `proposal-not-found`.
    """
    assert _slug_for_status(403)[0] == "insufficient-scope"
    assert _slug_for_status(404)[0] == "not-found"


def test_an_unknown_status_still_gets_a_uri_safe_slug() -> None:
    """Slugs land in a `type` URI, so everything outside [a-z0-9] is collapsed."""
    slug, _ = _slug_for_status(418)

    assert slug == "i-m-a-teapot"
    assert "'" not in slug


def test_rate_limited_floors_retry_after_at_one_second() -> None:
    """RFC 9110 wants an integer, and a sub-second remainder rounded down to 0
    invites a client to retry immediately and get denied again."""
    from air_platform.api.errors import RateLimitedError

    error = RateLimitedError("slow down", retry_after=0.2)

    assert error.headers["Retry-After"] == "1"
