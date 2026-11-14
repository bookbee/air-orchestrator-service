"""Shared builders for the air-tools-gateway client tests (spec 0012)."""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from air_orchestrator_service.clients.tools_gateway import ToolsGatewayClient
from air_orchestrator_service.config import GatewayKeyPair, ToolsGatewaySettings
from air_orchestrator_service.constants import Channel
from air_orchestrator_service.schemas.common import Principal
from tests.conftest import TOOLS_GATEWAY_BASE_URL

INVENTORY_URL = f"{TOOLS_GATEWAY_BASE_URL}/v1/capabilities"
READ_A = "airtg_read_tenant_a_never_rendered"
EXECUTE_A = "airtg_exec_tenant_a_never_rendered"
READ_B = "airtg_read_tenant_b_never_rendered"
EXECUTE_B = "airtg_exec_tenant_b_never_rendered"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def gateway_settings(**overrides: Any) -> ToolsGatewaySettings:
    values: dict[str, Any] = {
        "enabled": True,
        "base_url": TOOLS_GATEWAY_BASE_URL,
        "tenant_keys": {
            "tenant-a": GatewayKeyPair(read=SecretStr(READ_A), execute=SecretStr(EXECUTE_A)),
            "tenant-b": GatewayKeyPair(read=SecretStr(READ_B), execute=SecretStr(EXECUTE_B)),
        },
        "allow": {"customer": ["orders.*"]},
    }
    values.update(overrides)
    return ToolsGatewaySettings.model_validate(values)


def gateway_client(clock: Clock, **overrides: Any) -> ToolsGatewayClient:
    return ToolsGatewayClient(gateway_settings(**overrides), clock=clock)


def shopper(tenant: str = "tenant-a", subject: str | None = "cust_123") -> Principal:
    return Principal(
        key_id="customer", name="c", channel=Channel.CUSTOMER, tenant=tenant, subject=subject
    )


def read_entry(name: str, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "kind": "read",
        "version": 1,
        "requires_subject": True,
        "description": f"Look up {name}.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 10}},
        },
    }
    entry.update(overrides)
    return entry


def write_entry(name: str, **overrides: Any) -> dict[str, Any]:
    entry = read_entry(name, kind="write", risk="low")
    entry["input_schema"] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["order_id"],
        "properties": {"order_id": {"type": "string", "pattern": "^[0-9]{4,12}$"}},
    }
    entry.update(overrides)
    return entry


def inventory_body(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"service": "air-tools-gateway", "version": "0.1.0", "capabilities": list(entries)}
