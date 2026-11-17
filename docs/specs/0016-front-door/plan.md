# 0016 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Service side built; front door built in air-infrastructure-service, unproved until UAT · **Updated:** 2026-11-17

## 1. Approach

The service already behaves as the spec's **S** rows require; nothing in `src/` changes.
What was missing is the proof and the closed route sets, so this spec adds one test
module and cites four existing tests.

- `REQ-FDR-001`: the test holds the two sets and compares their union with the
  application's route table. The sets live in the test, not in `constants.py`: they are a
  statement about deployment, not a value the service uses at run time.
- `REQ-FDR-003`: the service never reads a client address. The entry point starts uvicorn
  without widening `forwarded_allow_ips`, so forwarded headers from a non-local peer are
  ignored by the server too. The test sends the headers and compares the outcome.
- `REQ-FDR-009`: there is no CORS middleware, and the test keeps it that way.

The **F** rows are implemented in the sibling repository, air-infrastructure-service,
under `gke/`:

| Requirement | Where |
| --- | --- |
| `002`, `007`, `010`, `011` | `gke/helm/air-service/templates/gateway.yaml` (a `Gateway`, an `HTTPRoute`, a `HealthCheckPolicy`, a `GCPBackendPolicy`), switched on in `gke/values/<env>/air-orchestrator-service.yaml`; the service's `NetworkPolicy` admits the load balancer's ranges |
| `011`, `012` | `gke/terraform/modules/internal-gateway/`: the proxy-only subnet, a static private address, and a regional Cloud Armor policy with the rate rule |
| proof | `gke/scripts/validate.sh` (render assertions) and `gke/scripts/smoke-front-door.sh` (UAT) |

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `tests/unit/test_front_door.py` | new | Three tests |
| `src/` | unchanged | The behaviour exists; the spec pins it |

## 3. Contract impact

- [x] No change to public contracts

## 4. Decisions taken

- **Backends only, over a private address** (product owner, 2026-11-17). No public
  address, no CORS, no browser.
- **GKE Gateway API**, internal class, in air-infrastructure-service. The spec stays
  product-neutral so a different front door needs no spec change.
- **The front door is the rate limit** until OR-017 is specified. Both are wanted in the
  end: the front door cannot see a key's own `rate_limit_rpm`.
- **Four times the turn deadline** for the response timeout (`REQ-FDR-010`): 60 s against
  the 15 s default. A streamed answer is written after the deadline's planning and
  gathering share, and a timeout equal to the deadline would cut answers that are on time.
- **No trusted-proxy client address.** Nothing needs it today; adding it means naming the
  proxy whose header is trusted, which is a requirement of its own.

## 5. Traceability

| Id | Test |
| --- | --- |
| `REQ-FDR-001` | `test_front_door.py::test_every_route_is_declared_public_or_internal` |
| `REQ-FDR-003` | `test_front_door.py::test_proxy_headers_never_change_who_the_caller_is` |
| `REQ-FDR-004` | `test_api_errors.py::test_a_supplied_request_id_is_echoed` |
| `REQ-FDR-005` | `test_events.py::test_the_stream_sets_the_no_buffering_header` |
| `REQ-FDR-006` | `test_system_routes.py::test_health_needs_no_auth_and_no_dependencies` |
| `REQ-FDR-008` | `test_api_errors.py::test_the_body_ceiling_is_a_400_malformed_request` |
| `REQ-FDR-009` | `test_front_door.py::test_a_cross_origin_preflight_is_not_answered` |
| `REQ-FDR-002`, `007`, `010`–`013` | Deferred to air-infrastructure-service (spec §10) |
