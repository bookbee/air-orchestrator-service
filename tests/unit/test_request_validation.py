"""Requests that cannot be answered as asked are refused up front — spec 0009 (``REQ-VAL-*``).

SIT bugs OR-004 (part 1) and OR-005 a, b, c, e. Each refusal must come before any
stage runs, so the gateway here fails the test if it is ever called.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from air_orchestrator_service.api.deps import STATE_ATTR
from tests.conftest import BUSINESS_KEY, CUSTOMER_KEY, auth


@pytest.fixture
def no_model(app: FastAPI) -> None:
    async def must_not_be_called(**_: object) -> Any:
        raise AssertionError("the model must not be called")

    gateway = getattr(app.state, STATE_ATTR).model_gateway
    gateway.generate = must_not_be_called
    gateway.stream = must_not_be_called


@pytest.mark.parametrize("accept", ["application/json", "text/event-stream"])
@pytest.mark.usefixtures("no_model")
async def test_an_unknown_session_is_a_404_before_anything_runs(
    client: httpx.AsyncClient, accept: str
) -> None:
    """REQ-VAL-001, OR-004's repro: a status, not a new session, on both shapes."""
    response = await client.post(
        "/v1/chat",
        headers={**auth(CUSTOMER_KEY), "Accept": accept},
        json={"message": "hi", "session_id": "sess_doesnotexist"},
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"].endswith("session-not-found")


async def test_another_tenants_session_is_the_same_404(client: httpx.AsyncClient) -> None:
    """REQ-VAL-001, Article IV: indistinguishable from an id that never existed."""
    created = await client.post("/v1/chat", headers=auth(CUSTOMER_KEY), json={"message": "hi"})
    session_id = created.json()["session_id"]

    theirs = await client.post(
        "/v1/query", headers=auth(BUSINESS_KEY), json={"query": "hi", "session_id": session_id}
    )
    unknown = await client.post(
        "/v1/query", headers=auth(BUSINESS_KEY), json={"query": "hi", "session_id": "sess_nope"}
    )

    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json()["detail"] == unknown.json()["detail"]


@pytest.mark.usefixtures("no_model")
@pytest.mark.parametrize(
    ("path", "key", "field"),
    [("/v1/chat", CUSTOMER_KEY, "message"), ("/v1/query", BUSINESS_KEY, "query")],
)
async def test_a_blank_message_is_a_422(
    client: httpx.AsyncClient, path: str, key: str, field: str
) -> None:
    """REQ-VAL-003 (OR-005a)."""
    response = await client.post(path, headers=auth(key), json={field: " \n\t "})

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"].endswith(field)


@pytest.mark.usefixtures("no_model")
@pytest.mark.parametrize(
    "schema",
    [
        {"type": "nonsense"},
        {"type": ["string", 3]},
        {"type": "object", "properties": []},
        {"type": "object", "properties": {"rate": {"type": "decimal"}}},
        {"type": "array", "items": {"type": "nonsense"}},
        {"type": "object", "required": "rate"},
    ],
)
async def test_an_invalid_output_schema_is_a_422(
    client: httpx.AsyncClient, schema: dict[str, Any]
) -> None:
    """REQ-VAL-004 (OR-005b)."""
    response = await client.post(
        "/v1/query", headers=auth(BUSINESS_KEY), json={"query": "rate?", "output_schema": schema}
    )

    assert response.status_code == 422
    assert "output_schema" in response.json()["errors"][0]["field"]


async def test_a_valid_output_schema_is_accepted(client: httpx.AsyncClient) -> None:
    schema = {
        "type": "object",
        "properties": {"rate": {"type": ["number", "null"]}, "tags": {"items": True}},
        "required": ["rate"],
    }
    response = await client.post(
        "/v1/query", headers=auth(BUSINESS_KEY), json={"query": "rate?", "output_schema": schema}
    )

    assert response.status_code == 200


@pytest.mark.usefixtures("no_model")
async def test_an_empty_route_list_is_a_422(client: httpx.AsyncClient) -> None:
    """REQ-VAL-005 (OR-005c)."""
    response = await client.post(
        "/v1/chat",
        headers=auth(CUSTOMER_KEY),
        json={"message": "hi", "options": {"allow_routes": []}},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("key", [None, "airo_not_a_real_key"])
@pytest.mark.parametrize("body", [b"{bad", b'{"unexpected": 1}'])
async def test_an_unauthenticated_bad_body_is_a_401(
    client: httpx.AsyncClient, key: str | None, body: bytes
) -> None:
    """REQ-VAL-006 (OR-005e): who the caller is comes before what they sent."""
    headers = {"Content-Type": "application/json", **(auth(key) if key else {})}

    response = await client.post("/v1/chat", headers=headers, content=body)

    assert response.status_code == 401


async def test_an_authenticated_bad_body_is_still_a_422(client: httpx.AsyncClient) -> None:
    """A key-holder sees the field errors. (A body that is not JSON at all is a 400,
    ``REQ-VAL-008``.)"""
    response = await client.post(
        "/v1/chat",
        headers={"Content-Type": "application/json", **auth(CUSTOMER_KEY)},
        content=b'{"unexpected": 1}',
    )

    assert response.status_code == 422


@pytest.mark.usefixtures("no_model")
@pytest.mark.parametrize("approve", ["yes", "true", "on", 1, "1"])
async def test_approve_must_be_a_json_boolean(client: httpx.AsyncClient, approve: Any) -> None:
    """REQ-MUT-015 (OR-013a): consent is a JSON boolean, never coerced from text."""
    response = await client.post(
        "/v1/chat",
        headers=auth(CUSTOMER_KEY),
        json={
            "message": "yes",
            "confirm": {"proposal_id": "prop_000000000000", "approve": approve},
        },
    )

    assert response.status_code == 422
    assert response.json()["errors"][0]["field"] == "confirm.approve"


@pytest.mark.usefixtures("no_model")
async def test_a_body_that_is_not_json_is_a_malformed_request(client: httpx.AsyncClient) -> None:
    """REQ-VAL-008 (OR-013b): 400 malformed-request, not a 422 naming a byte offset."""
    response = await client.post(
        "/v1/chat",
        headers={"Content-Type": "application/json", **auth(CUSTOMER_KEY)},
        content=b'{"message": "hi",',
    )

    assert response.status_code == 400
    assert response.json()["type"].endswith("/malformed-request")
    assert response.json()["errors"] == []
