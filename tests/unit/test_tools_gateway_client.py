"""air-tools-gateway client calls: per-tenant keys, the envelope, the metric
(spec 0012, ``REQ-TGW-007``/``022``)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import respx

from air_orchestrator_service.clients.tools_gateway import (
    Capability,
    ToolsGatewayClient,
    ToolsGatewayError,
)
from air_orchestrator_service.constants import HEADER_API_KEY
from air_orchestrator_service.observability import metrics
from tests.conftest import TOOLS_GATEWAY_BASE_URL
from tests.unit._tools_gateway_support import (
    EXECUTE_A,
    EXECUTE_B,
    INVENTORY_URL,
    READ_A,
    READ_B,
    Clock,
    gateway_client,
    inventory_body,
    read_entry,
    write_entry,
)

_LOOKUP_URL = f"{TOOLS_GATEWAY_BASE_URL}/v1/tools/orders.list_recent"
_PROPOSE_URL = f"{TOOLS_GATEWAY_BASE_URL}/v1/actions/orders.cancel/propose"


@pytest.fixture
async def gateway() -> AsyncIterator[ToolsGatewayClient]:
    client = gateway_client(Clock())
    yield client
    await client.aclose()


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


def _by_key(request: httpx.Request) -> httpx.Response:
    names = {READ_A: "orders.list_recent", READ_B: "orders.get"}
    name = names[request.headers[HEADER_API_KEY]]
    return httpx.Response(200, json=inventory_body(read_entry(name), write_entry("orders.cancel")))


async def _capability(gateway: ToolsGatewayClient, name: str) -> Capability:
    capability = (await gateway.inventory("tenant-a", timeout_s=1.0)).get(name)
    assert capability is not None
    return capability


def _result(name: str) -> dict[str, object]:
    return {
        "capability": name,
        "data": {"orders": []},
        "truncated": False,
        "as_of": "2026-11-14T10:02:11Z",
    }


async def test_each_tenant_is_called_with_its_own_key(
    gateway: ToolsGatewayClient, mock: respx.MockRouter
) -> None:
    """``REQ-TGW-007``. Two tenants get two inventories, each fetched with its own read
    key; a lookup carries the tenant's read key and the subject in the envelope; a
    tenant with no key pair gets nothing and no request. The execute keys never
    leave on this client."""
    inventory = mock.get(INVENTORY_URL).mock(side_effect=_by_key)
    lookup = mock.post(_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json=_result("orders.list_recent"))
    )

    a = await gateway.inventory("tenant-a", timeout_s=1.0)
    b = await gateway.inventory("tenant-b", timeout_s=1.0)
    assert a.get("orders.list_recent") is not None and a.get("orders.get") is None
    assert b.get("orders.get") is not None and b.get("orders.list_recent") is None
    assert len((await gateway.inventory("tenant-c", timeout_s=1.0)).capabilities) == 0
    assert inventory.call_count == 2

    capability = await _capability(gateway, "orders.list_recent")
    result = await gateway.run(
        capability, {"limit": 3}, tenant="tenant-a", subject="cust_123", timeout_s=1.0
    )
    assert result.capability == "orders.list_recent"
    sent = lookup.calls.last.request
    assert sent.headers[HEADER_API_KEY] == READ_A
    assert json.loads(sent.content) == {"arguments": {"limit": 3}, "subject": "cust_123"}

    every_key = [call.request.headers[HEADER_API_KEY] for call in mock.calls]
    assert EXECUTE_A not in every_key and EXECUTE_B not in every_key
    assert READ_A not in repr(gateway) and EXECUTE_A not in repr(gateway)


async def test_a_capability_without_a_customer_is_sent_no_subject(
    gateway: ToolsGatewayClient, mock: respx.MockRouter
) -> None:
    mock.get(INVENTORY_URL).mock(
        return_value=httpx.Response(
            200, json=inventory_body(read_entry("orders.list_recent", requires_subject=False))
        )
    )
    lookup = mock.post(_LOOKUP_URL).mock(
        return_value=httpx.Response(200, json=_result("orders.list_recent"))
    )

    capability = await _capability(gateway, "orders.list_recent")
    await gateway.run(capability, {}, tenant="tenant-a", subject="cust_123", timeout_s=1.0)

    assert json.loads(lookup.calls.last.request.content)["subject"] is None


def _count(capability: str, kind: str, outcome: str) -> float:
    value = metrics.REGISTRY.get_sample_value(
        "air_orchestrator_service_capability_calls_total",
        {"capability": capability, "kind": kind, "outcome": outcome},
    )
    return value or 0.0


async def test_every_capability_call_is_counted(
    gateway: ToolsGatewayClient, mock: respx.MockRouter
) -> None:
    """``REQ-TGW-022``: one sample per call, labelled by capability, kind and outcome,
    including a response that names another capability."""
    mock.get(INVENTORY_URL).mock(side_effect=_by_key)
    mock.post(_LOOKUP_URL).mock(
        side_effect=[
            httpx.Response(200, json=_result("orders.list_recent")),
            httpx.Response(503),
            httpx.ReadTimeout("slow"),
            httpx.Response(200, json=_result("orders.get")),
        ]
    )
    mock.post(_PROPOSE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "proposal_id": "gp_7c1e4a90b2d3",
                "capability": "orders.cancel",
                "risk": "low",
                "summary": "Cancel order 55120.",
                "expires_at": "2026-11-14T10:12:11Z",
            },
        )
    )
    lookup = await _capability(gateway, "orders.list_recent")
    cancel = await _capability(gateway, "orders.cancel")
    before = {
        outcome: _count("orders.list_recent", "read", outcome)
        for outcome in ("ok", "error", "timeout")
    }
    proposed_before = _count("orders.cancel", "write", "ok")

    async def run() -> None:
        await gateway.run(lookup, {}, tenant="tenant-a", subject="cust_123", timeout_s=1.0)

    await run()
    for _ in range(3):
        with pytest.raises(ToolsGatewayError):
            await run()
    proposal = await gateway.propose(
        cancel, {"order_id": "55120"}, tenant="tenant-a", subject="cust_123", timeout_s=1.0
    )

    assert _count("orders.list_recent", "read", "ok") == before["ok"] + 1
    assert _count("orders.list_recent", "read", "error") == before["error"] + 2
    assert _count("orders.list_recent", "read", "timeout") == before["timeout"] + 1
    assert _count("orders.cancel", "write", "ok") == proposed_before + 1
    assert proposal.proposal_id == "gp_7c1e4a90b2d3"


async def test_arguments_are_checked_against_the_schema_without_echoing_them(
    gateway: ToolsGatewayClient, mock: respx.MockRouter
) -> None:
    """Groundwork for ``REQ-TGW-006``: the compiled validator, and messages that name
    the path, never the value a model produced."""
    mock.get(INVENTORY_URL).mock(side_effect=_by_key)
    cancel = await _capability(gateway, "orders.cancel")

    assert cancel.argument_errors({"order_id": "55120"}) == []
    errors = cancel.argument_errors({"order_id": "DROP TABLE", "subject": "cust_999"})
    assert errors
    assert not any("DROP" in e or "cust_999" in e for e in errors)
