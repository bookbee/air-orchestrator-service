"""A double of air-tools-gateway that enforces its contract — spec 0012, ``REQ-TGW-024``.

The unit tests mock the gateway loosely, one route at a time. This double instead
loads ``docs/specs/0012-tools-gateway/contract.openapi.yaml`` and holds every request
to it: the request body schemas (``additionalProperties: false``, so an unknown field
is refused), the path's capability name, ``X-Request-ID`` and ``Idempotency-Key``, the
capability's own ``input_schema``, ``requires_subject``, and the two key scopes. It
answers the way the contract says the real gateway does: ``401`` for an unknown key,
``403`` for a key outside its scope, ``400 invalid_arguments``, ``404`` for an unknown
capability or someone else's proposal, ``409 already_executed`` for a second
``Idempotency-Key``, and the first response again for the same one.

**Why it serves the contract's own examples.** The inventory, the lookup result, the
proposal and the execute outcome are the examples in the contract file. The client's
inventory checks then run against what the gateway's builders were shown, and an
example that drifts from its schema is caught too: every response the double sends is
validated against the contract's response schema.

**Why a refusal is recorded, not only answered.** The engine turns a gateway ``400``
into a ``degraded`` turn that still answers, so a request the contract refuses would
otherwise pass a test unnoticed. :attr:`ToolsGatewayDouble.refused` lists every
refusal, and a test of a real flow asserts it is empty.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
import respx
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

CONTRACT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2] / "docs/specs/0012-tools-gateway/contract.openapi.yaml"
)
_CONTRACT_URI: Final[str] = "urn:air-tools-gateway:contract"

_TOOL: Final = re.compile(r"^/v1/tools/(?P<name>[^/]+)$")
_ACTION: Final = re.compile(r"^/v1/actions/(?P<name>[^/]+)/(?P<verb>propose|execute)$")


def load_contract() -> dict[str, Any]:
    contract: dict[str, Any] = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    return contract


@dataclass(frozen=True)
class Refusal:
    """One request the double refused, and why. ``reason`` is for the test's failure
    message; ``code`` is the contract's ``ProblemCode``."""

    method: str
    path: str
    status: int
    code: str
    reason: str


@dataclass
class _Proposal:
    tenant: str
    subject: str | None
    capability: str
    arguments: dict[str, Any]
    executed_with: str | None = None


@dataclass
class ToolsGatewayDouble:
    """Mount with :meth:`mount` on a respx router. ``keys`` maps each tenant to its
    ``(read, execute)`` pair."""

    base_url: str
    keys: dict[str, tuple[str, str]]
    contract: dict[str, Any] = field(default_factory=load_contract)
    #: What ``POST .../execute`` answers next, as an ``ExecuteResponse`` ``status``.
    execute_status: str = "done"
    refused: list[Refusal] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    _proposals: dict[str, _Proposal] = field(default_factory=dict)
    _replies: dict[str, dict[str, Any]] = field(default_factory=dict)
    _validators: dict[str, Draft202012Validator] = field(default_factory=dict)

    # ── Wiring ────────────────────────────────────────────────────────────────

    def mount(self, router: respx.MockRouter) -> None:
        router.route(url__startswith=f"{self.base_url}/v1/").mock(side_effect=self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in ("/v1/health", "/v1/ready"):
            return httpx.Response(200, json={"status": "ok"})
        scope = self._scope(request)
        if scope is None:
            return self._refuse(request, 401, "unauthorized", "missing or unknown X-API-Key")
        tenant, kind = scope
        request_id = request.headers.get("X-Request-ID")
        if request_id is not None and self._errors("RequestIdHeader", request_id):
            return self._refuse(request, 400, "invalid_arguments", "X-Request-ID")

        if request.method == "GET" and path == "/v1/capabilities":
            if kind != "read":
                return self._refuse(request, 403, "forbidden", "execute key on capabilities")
            return self._reply(200, self._inventory(), "Inventory")
        if request.method != "POST":
            return self._refuse(request, 404, "unknown_capability", "no such operation")
        if match := _TOOL.match(path):
            return self._tool(request, tenant, kind, match["name"])
        if match := _ACTION.match(path):
            if match["verb"] == "propose":
                return self._propose(request, tenant, kind, match["name"])
            return self._execute(request, tenant, kind, match["name"])
        return self._refuse(request, 404, "unknown_capability", "no such operation")

    # ── Operations ────────────────────────────────────────────────────────────

    def _tool(self, request: httpx.Request, tenant: str, kind: str, name: str) -> httpx.Response:
        if kind != "read":
            return self._refuse(request, 403, "forbidden", "execute key on a tool")
        checked = self._call_body(request, name, "read")
        if isinstance(checked, httpx.Response):
            return checked
        result = dict(self._example("/v1/tools/{name}", "200"))
        result["capability"] = name
        return self._reply(200, result, "ToolResult")

    def _propose(self, request: httpx.Request, tenant: str, kind: str, name: str) -> httpx.Response:
        if kind != "read":
            return self._refuse(request, 403, "forbidden", "execute key on propose")
        checked = self._call_body(request, name, "write")
        if isinstance(checked, httpx.Response):
            return checked
        proposal = dict(self._example("/v1/actions/{name}/propose", "200"))
        proposal["capability"] = name
        proposal["proposal_id"] = f"gp_{len(self._proposals) + 1:012x}"
        # The example's date is fixed; the contract allows at most 10 minutes.
        proposal["expires_at"] = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        self._proposals[proposal["proposal_id"]] = _Proposal(
            tenant=tenant,
            subject=checked["subject"],
            capability=name,
            arguments=checked["arguments"],
        )
        return self._reply(200, proposal, "ProposalResponse")

    def _execute(self, request: httpx.Request, tenant: str, kind: str, name: str) -> httpx.Response:
        if kind != "execute":
            return self._refuse(request, 403, "forbidden", "read key on execute")
        key = request.headers.get("Idempotency-Key")
        if key is None or self._errors("IdempotencyKeyHeader", key):
            return self._refuse(request, 400, "invalid_arguments", "Idempotency-Key")
        if self._errors("CapabilityName", name):
            return self._refuse(request, 404, "unknown_capability", "capability name")
        body = self._json(request)
        if body is None or self._errors("ExecuteRequest", body):
            return self._refuse(request, 400, "invalid_arguments", "ExecuteRequest body")
        if key in self._replies:
            return httpx.Response(200, json=self._replies[key])
        proposal = self._proposals.get(body["proposal_id"])
        if (
            proposal is None
            or proposal.tenant != tenant
            or proposal.subject != body["subject"]
            or proposal.capability != name
        ):
            return self._refuse(request, 404, "not_found", "no such proposal for this customer")
        if proposal.executed_with is not None:
            return self._refuse(request, 409, "already_executed", "another Idempotency-Key")
        proposal.executed_with = key
        outcome: dict[str, Any] = {
            "status": self.execute_status,
            "execution_id": f"gx_{len(self._replies) + 1:08x}",
            "capability": name,
        }
        if self.execute_status == "done":
            outcome["result"] = {"order_id": proposal.arguments.get("order_id")}
        elif self.execute_status == "rejected":
            outcome["code"] = "invalid_state"
        self._replies[key] = outcome
        return self._reply(200, outcome, "ExecuteResponse")

    # ── Checks ────────────────────────────────────────────────────────────────

    def _call_body(
        self, request: httpx.Request, name: str, kind: str
    ) -> dict[str, Any] | httpx.Response:
        """A ``ToolRequest`` checked against the contract and the capability."""
        if self._errors("CapabilityName", name):
            return self._refuse(request, 404, "unknown_capability", "capability name")
        capability = next((c for c in self._inventory()["capabilities"] if c["name"] == name), None)
        if capability is None or capability["kind"] != kind:
            return self._refuse(request, 404, "unknown_capability", f"no {kind} {name}")
        body = self._json(request)
        if body is None or self._errors("ToolRequest", body):
            return self._refuse(request, 400, "invalid_arguments", "ToolRequest body")
        if list(Draft202012Validator(capability["input_schema"]).iter_errors(body["arguments"])):
            return self._refuse(request, 400, "invalid_arguments", f"{name} arguments")
        if capability["requires_subject"] and body["subject"] is None:
            return self._refuse(request, 400, "invalid_arguments", f"{name} needs a subject")
        checked: dict[str, Any] = body
        return checked

    def _scope(self, request: httpx.Request) -> tuple[str, str] | None:
        """``(tenant, "read" | "execute")`` for the request's key, or ``None``."""
        sent = request.headers.get_list("X-API-Key")
        if len(sent) != 1:
            return None
        for tenant, (read, execute) in self.keys.items():
            if sent[0] == read:
                return tenant, "read"
            if sent[0] == execute:
                return tenant, "execute"
        return None

    def _errors(self, schema: str, value: Any) -> list[str]:
        return [e.message for e in self._validator(schema).iter_errors(value)]

    def _validator(self, schema: str) -> Draft202012Validator:
        if schema not in self._validators:
            headers = {
                "RequestIdHeader": self.contract["components"]["parameters"]["RequestId"]["schema"],
                "IdempotencyKeyHeader": next(
                    p["schema"]
                    for p in self.contract["paths"]["/v1/actions/{name}/execute"]["post"][
                        "parameters"
                    ]
                    if p.get("name") == "Idempotency-Key"
                ),
            }
            root = headers.get(schema, {"$ref": f"{_CONTRACT_URI}#/components/schemas/{schema}"})
            registry: Registry[Any] = Registry().with_resource(
                _CONTRACT_URI,
                Resource.from_contents(self.contract, default_specification=DRAFT202012),
            )
            self._validators[schema] = Draft202012Validator(root, registry=registry)
        return self._validators[schema]

    # ── Responses ─────────────────────────────────────────────────────────────

    def _inventory(self) -> dict[str, Any]:
        inventory: dict[str, Any] = self._example("/v1/capabilities", "200", method="get")
        return inventory

    def _example(self, path: str, status: str, *, method: str = "post") -> dict[str, Any]:
        content = self.contract["paths"][path][method]["responses"][status]["content"]
        example: dict[str, Any] = json.loads(json.dumps(content["application/json"]["example"]))
        return example

    def _reply(self, status: int, body: dict[str, Any], schema: str) -> httpx.Response:
        """The double's own answer, held to the contract like a request is."""
        if errors := self._errors(schema, body):
            raise AssertionError(f"the double's {schema} breaks the contract: {errors}")
        return httpx.Response(status, json=body)

    def _refuse(
        self, request: httpx.Request, status: int, code: str, reason: str
    ) -> httpx.Response:
        self.refused.append(Refusal(request.method, request.url.path, status, code, reason))
        problem = {
            "type": f"https://air.local/problems/{code}",
            "title": reason,
            "status": status,
            "code": code,
        }
        if errors := self._errors("Problem", problem):
            raise AssertionError(f"the double's Problem breaks the contract: {errors}")
        return httpx.Response(
            status, json=problem, headers={"Content-Type": "application/problem+json"}
        )

    @staticmethod
    def _json(request: httpx.Request) -> Any:
        try:
            return json.loads(request.content)
        except ValueError:
            return None
