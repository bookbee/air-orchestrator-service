# 0010 — Optional downstreams share one client, one breaker, and live status

**Status:** Implemented · **Area:** `CLB` · **Phase:** 3 (read path) · **Updated:** 2026-11-14
**Derives from:** [HLD §7 (degradation)](../../01-hld.md) · [LLD §1, §5](../../02-lld.md) ·
[Plan §5, Phase 3](../../00-plan.md) · spec [0005](../0005-classifier-client/spec.md) `REQ-CLS-013`
**Supersedes:** —

## 1. Problem

air-classifier is the only optional downstream with a client today, and that client carries
its own copy of everything a downstream call needs: the key header, the request id, the
failure taxonomy, the metric, and the rule that no URL or body reaches an error message.
air-rag-service is next and air-tools-gateway after it. A third copy of that code is how one of them ends up
rendering a URL into a stage detail. Nothing stops a failing downstream from being called on
every turn, either. A dead air-classifier costs each customer turn its full timeout share
until someone notices. `/v1/capabilities` lists a route as available whenever its service is
*configured*, even when the service has been down all day.

## 2. Outcome

Every optional downstream is called through one shared client. It sends the same headers,
reports failures in the same four words, records the same metric, and never echoes a URL or
upstream body. Each service has a circuit breaker. After repeated failures it stops calling
that service for a cooldown, and turns degrade at once instead of waiting out a timeout.
`/v1/capabilities` probes each configured optional service and lists a route only when its
service answers and its breaker is closed. `/v1/ready` keeps its current behaviour.

## 3. Scope

**In scope**

- The shared calling behaviour for optional downstreams (air-classifier now, air-rag-service in spec
  0011, air-tools-gateway in spec 0012).
- A per-service circuit breaker.
- Live status of optional downstreams on `/v1/capabilities`.

**Out of scope**

- **Retries.** A turn has one deadline, and the downstream calls inside it are given a share
  of it. A retry would spend the share again, on a service that just failed. The breaker is
  what protects a struggling service.
- **air-model-gateway and air-infra.** Both are mandatory, have their own clients, and the
  gateway gates readiness. They keep their own clients.
- **Reading each service's `/v1/capabilities` body** to build a tool inventory. That needs
  air-tools-gateway's contract and belongs to spec 0012.
- **Probing optional services from `/v1/ready`.** Spec 0005 `REQ-CLS-010` and Article III keep
  readiness gated on the gateway alone.

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| III — A missing downstream narrows | A breaker turns a slow failure into a fast one | An open breaker is a failure like any other: the stage degrades, the turn answers |
| VI — Public contracts are closed sets | `/v1/capabilities` gains a field | Additive: `downstreams`. The breaker's outcome `breaker_open` is already in the metric's documented vocabulary |
| IX — Nothing renders secrets | One place builds every downstream error | Errors carry a reason word and a status code, never a URL, key or body |
| VIII — Nothing is a module-level singleton | A breaker is state shared across requests | Each breaker lives on its client, which lives on `AppState`. A test builds a fresh app and gets fresh breakers |
| I, II, IV, V, VII, X | Unaffected | — |

## 5. Requirements

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-CLB-001` | MUST | A call to an optional downstream sends `X-API-Key` when a key is configured, and the turn's `X-Request-ID` when one is set, and is attempted once. |
| `REQ-CLB-002` | MUST | Every failed call is reported as exactly one of `timeout`, `unavailable` (connection failed), `error` (non-2xx, or a body that does not parse) or `breaker_open`, and recorded on `air_orchestrator_service_downstream_calls_total` with that outcome. A success is recorded as `ok`. |
| `REQ-CLB-003` | MUST NOT | A downstream error's message never contains the service's URL, a key, or any part of the response body. |
| `REQ-CLB-004` | MUST | After `breaker_threshold` consecutive failures (default 5) a service's breaker opens. While it is open, calls to that service fail at once with `breaker_open` and make no network request. |
| `REQ-CLB-005` | MUST | Once `breaker_cooldown_s` (default 30) has passed, one call is let through. Its success closes the breaker, and its failure reopens it for another cooldown. |
| `REQ-CLB-006` | MUST NOT | A call abandoned by its caller (the client disconnected, or the turn was cancelled) does not count as a failure. |
| `REQ-CLB-007` | MUST | `/v1/capabilities` reports each configured optional downstream's live status, probed through `GET /v1/health` within the service's health timeout. The probes run concurrently, and an open breaker is reported without probing. `routes` omits a route whose service is unreachable or whose breaker is open. |
| `REQ-CLB-008` | MUST NOT | `/v1/ready` probes no optional downstream. It reports them from configuration as before. |

## 6. Acceptance criteria

**AC-1 — A dead classifier stops costing time** (`REQ-CLB-004`)
- **Given** air-classifier refusing connections, and the default threshold
- **When** six customer turns run
- **Then** the first five each try the classifier and degrade `CLASSIFY`. The sixth degrades it
  with `breaker_open` and makes no request

**AC-2 — Capabilities tell the truth** (`REQ-CLB-007`)
- **Given** air-rag-service configured but down
- **When** an admin key calls `/v1/capabilities`
- **Then** `downstreams` shows `air-rag` `reachable: false`, and `routes` does not contain `rag`

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| Breaker open | The stage that would call the service degrades with `breaker_open`, naming the service | As that stage's spec decides (0005: unaffected; 0011: `degraded`) |
| Health probe fails on `/v1/capabilities` | `reachable: false` with a reason word; the route drops out | — |

## 8. Observability

- `air_orchestrator_service_downstream_calls_total{service, outcome}` for every call, with
  `breaker_open` for a call that was refused without being made.
- Log line `downstream.breaker` with `service`, `from_state` and `to_state` on every
  transition.

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| — | None open. | — | — |

## 10. Traceability

| Id | Proof |
| --- | --- |
| `REQ-CLB-001` | `tests/unit/test_client_base.py::test_a_call_sends_the_key_and_request_id_once` |
| `REQ-CLB-002` | `tests/unit/test_client_base.py::test_each_failure_is_one_reason_and_one_metric` |
| `REQ-CLB-003` | `tests/unit/test_client_base.py::test_an_error_never_carries_the_url_or_body` |
| `REQ-CLB-004` | `tests/unit/test_client_base.py::test_the_breaker_opens_after_the_threshold` · `tests/unit/test_classify_stage.py::test_an_open_breaker_degrades_classify_without_a_call` |
| `REQ-CLB-005` | `tests/unit/test_client_base.py::test_after_the_cooldown_one_probe_decides` |
| `REQ-CLB-006` | `tests/unit/test_client_base.py::test_a_cancelled_call_is_not_a_failure` |
| `REQ-CLB-007` | `tests/unit/test_system_routes.py::test_capabilities_report_live_downstream_status` |
| `REQ-CLB-008` | `tests/unit/test_system_routes.py::test_ready_does_not_probe_optional_downstreams` |
