"""air-tools-gateway's inventory: per tenant, checked entry by entry, cached with
grace (spec 0012, ``REQ-TGW-001``/``002``/``003``)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import respx

from air_orchestrator_service.clients.tools_gateway import (
    MAX_CAPABILITIES,
    Inventory,
    ToolsGatewayClient,
)
from air_orchestrator_service.constants import Channel
from air_orchestrator_service.schemas.common import Principal
from tests.unit._tools_gateway_support import (
    INVENTORY_URL,
    Clock,
    gateway_client,
    inventory_body,
    read_entry,
    shopper,
    write_entry,
)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def gateway(clock: Clock) -> AsyncIterator[ToolsGatewayClient]:
    client = gateway_client(clock)
    yield client
    await client.aclose()


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


async def _offered(gateway: ToolsGatewayClient, principal: Principal | None = None) -> list[str]:
    who = principal or shopper()
    inventory = await gateway.inventory(who.tenant, timeout_s=1.0)
    return [c.name for c in inventory.offered_to(who, gateway.settings).capabilities]


async def test_a_new_capability_in_an_allowed_namespace_needs_no_change(
    gateway: ToolsGatewayClient, mock: respx.MockRouter, clock: Clock
) -> None:
    """AC-1. ``orders.*`` is allowed; ``orders.get`` appears at the gateway between two
    refreshes and is offered with no change here. Inside the TTL nothing is refetched,
    and a namespace the allow-list does not name is never offered."""
    route = mock.get(INVENTORY_URL).mock(
        side_effect=[
            httpx.Response(200, json=inventory_body(read_entry("orders.list_recent"))),
            httpx.Response(
                200,
                json=inventory_body(
                    read_entry("orders.list_recent"),
                    read_entry("orders.get"),
                    read_entry("payments.list"),
                ),
            ),
        ]
    )

    assert await _offered(gateway) == ["orders.list_recent"]
    clock.now += gateway.settings.inventory_ttl_s - 1
    assert await _offered(gateway) == ["orders.list_recent"]
    assert route.call_count == 1

    clock.now += 1
    assert await _offered(gateway) == ["orders.list_recent", "orders.get"]
    assert route.call_count == 2


async def test_what_a_turn_is_offered_follows_channel_and_subject(
    gateway: ToolsGatewayClient, mock: respx.MockRouter
) -> None:
    """``REQ-TGW-004``/``026``: the channel's allow-list, never a write on the business
    channel, and nothing customer-shaped for a turn without a customer."""
    mock.get(INVENTORY_URL).mock(
        return_value=httpx.Response(
            200,
            json=inventory_body(
                read_entry("orders.list_recent"),
                write_entry("orders.cancel"),
                read_entry("orders.catalogue_search", requires_subject=False),
            ),
        )
    )
    gateway.settings.allow.business.append("orders.*")
    analyst = Principal(key_id="b", name="b", channel=Channel.BUSINESS, tenant="tenant-a")

    assert await _offered(gateway) == [
        "orders.list_recent",
        "orders.cancel",
        "orders.catalogue_search",
    ]
    assert await _offered(gateway, shopper(subject=None)) == ["orders.catalogue_search"]
    assert await _offered(gateway, analyst) == ["orders.catalogue_search"]


_BAD_ENTRIES: list[tuple[str, dict[str, Any]]] = [
    ("name", read_entry("Orders.Get")),
    ("name", read_entry("orders")),
    ("name", read_entry("orders." + "x" * 64)),
    ("kind", read_entry("orders.bad", kind="delete")),
    ("version", read_entry("orders.bad", version=0)),
    ("description", read_entry("orders.bad", description="x" * 1001)),
    ("requires_subject", read_entry("orders.bad", requires_subject="yes")),
    ("risk", write_entry("orders.bad", risk="none")),
    ("input_schema", read_entry("orders.bad", input_schema={"type": "object"})),
    (
        "input_schema",
        read_entry(
            "orders.bad",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"subject": {"type": "string"}},
            },
        ),
    ),
    (
        "input_schema",
        read_entry(
            "orders.bad",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"tenant": {"type": "string"}},
            },
        ),
    ),
    (
        "input_schema",
        read_entry(
            "orders.bad",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "patternProperties": {"^sub": {"type": "string"}},
            },
        ),
    ),
    (
        "input_schema",
        read_entry(
            "orders.bad",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"limit": {"type": "integr"}},
            },
        ),
    ),
]


@pytest.mark.parametrize(("reason", "bad"), _BAD_ENTRIES, ids=[r for r, _ in _BAD_ENTRIES])
async def test_a_malformed_entry_is_dropped_and_the_rest_kept(
    gateway: ToolsGatewayClient,
    mock: respx.MockRouter,
    clock: Clock,
    caplog: pytest.LogCaptureFixture,
    reason: str,
    bad: dict[str, Any],
) -> None:
    """``REQ-TGW-002``: dropped alone, and logged once by name across refreshes."""
    mock.get(INVENTORY_URL).mock(
        return_value=httpx.Response(200, json=inventory_body(bad, read_entry("orders.get")))
    )

    with caplog.at_level(logging.WARNING):
        assert await _offered(gateway) == ["orders.get"]
        clock.now += gateway.settings.inventory_ttl_s
        assert await _offered(gateway) == ["orders.get"]

    assert caplog.text.count("tools.inventory.entry_dropped") == 1
    assert f"'reason': '{reason}'" in caplog.text


async def test_at_most_64_entries_are_kept_and_a_duplicate_is_dropped(
    gateway: ToolsGatewayClient, mock: respx.MockRouter
) -> None:
    """``REQ-TGW-002``: the bound that keeps the metric label's cardinality finite."""
    entries = [read_entry("orders.get")] + [
        read_entry(f"orders.lookup_{i}") for i in range(MAX_CAPABILITIES + 5)
    ]
    mock.get(INVENTORY_URL).mock(return_value=httpx.Response(200, json=inventory_body(*entries)))

    inventory = await gateway.inventory("tenant-a", timeout_s=1.0)

    names = [c.name for c in inventory.capabilities]
    assert len(names) == MAX_CAPABILITIES
    assert len(set(names)) == len(names)


async def test_a_failed_fetch_leaves_no_capabilities_and_the_turn_answers(
    gateway: ToolsGatewayClient, mock: respx.MockRouter, clock: Clock
) -> None:
    """``REQ-TGW-003``, the client half (the turn half is T6). A failed refresh keeps
    the last good inventory until the grace runs out from its fetch, then serves none.
    Nothing raises, and a failure is not retried inside the TTL."""
    ttl, grace = gateway.settings.inventory_ttl_s, gateway.settings.inventory_grace_s
    route = mock.get(INVENTORY_URL).mock(
        side_effect=[
            httpx.Response(200, json=inventory_body(read_entry("orders.get"))),
            *[httpx.Response(503)] * 10,
        ]
    )

    assert await _offered(gateway) == ["orders.get"]
    clock.now += ttl
    assert await _offered(gateway) == ["orders.get"]  # refresh failed, inside grace
    assert await _offered(gateway) == ["orders.get"]  # and is not retried at once
    assert route.call_count == 2

    clock.now += grace - ttl
    assert await _offered(gateway) == []
    assert (await gateway.inventory("tenant-a", timeout_s=1.0)).available is False
    assert route.call_count == 3


async def test_a_cold_cache_that_cannot_fetch_is_empty(
    gateway: ToolsGatewayClient, mock: respx.MockRouter
) -> None:
    mock.get(INVENTORY_URL).mock(side_effect=httpx.ConnectError("refused"))

    assert await gateway.inventory("tenant-a", timeout_s=1.0) == Inventory(available=False)
