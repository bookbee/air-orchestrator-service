# 0016 — The front door

**Status:** Partial (the service's side is built and proved; the front door's side is deployed by air-infrastructure-service and proved in UAT) · **Area:** `FDR` · **Phase:** 6 (hardening and ops) · **Updated:** 2026-11-17
**Derives from:** HLD §2 (context), LLD §4 (event contract), §12 (errors); Plan §4 (two channels)
**Supersedes:** —

## 1. Problem

Deployed, this service is reachable only from inside its cluster: nothing lets a client
in. Whatever is put in front of it becomes part of the service's security and of its
streaming contract, and until now nothing said what that thing must do, what it must not
do, or which of the service's own behaviours it may rely on. Two facts make this more
than a deployment detail. The configured rate limit is never enforced (air-web-console
OR-017), so the front door is the only limit there is. And an answer is streamed: a
front door that buffers turns a live answer into one late block.

## 2. Outcome

There is one written contract between this service and whatever stands in front of it.
The service's half is proved by tests here: which routes exist, that identity never
comes from a proxy header, that a stream declares itself unbufferable, that liveness
needs nothing, that an oversized body is refused whoever forwards it, and that no
browser is invited. The front door's half is a list of requirements that
air-infrastructure-service's deployment implements and its smoke test proves. Decided
2026-11-17: the only callers are the shop's backends, over a private address; the front
door is an internal load balancer.

## 3. Scope

**In scope**

- Which routes are public and which are internal.
- What the service trusts from a front door, and what it never does.
- What a front door must do: TLS, routing, health checking, streaming, timeouts, who may
  connect, rate limiting.

**Out of scope**

- The product used. This spec names none; the implementation is a GKE Gateway (an
  internal Application Load Balancer) in air-infrastructure-service's `gke/`, and its
  runbook §5.6 is where the choices are recorded.
- Enforcing the service's own rate limit (OR-017). `REQ-FDR-012` makes the front door
  the limit until that is specified and built; it does not replace it.
- Browser clients. The API key must never reach a browser, so there is no CORS and no
  public address. A browser-facing product puts its own backend in front, as
  air-web-console's does.
- Durable sessions (spec 0002). One replica behind the front door is still one replica.
- Client-address logging. The service records no client address today (`REQ-FDR-003`);
  if audit ever needs one, that is a new requirement with a named trusted proxy.

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| I — The channel comes from the credential | A front door adds headers, and a header that could name a channel, a tenant or a customer would let a caller choose its guardrail profile | Nothing a proxy sets is read for identity: only `X-API-Key` and `air-token` (`REQ-FDR-003`) |
| III — A missing downstream narrows the answer | A health check on readiness would take the service out of rotation whenever air-model-gateway blinked, and turn a degraded answer into no answer | The front door checks liveness, which depends on nothing (`REQ-FDR-006`, `007`) |
| VI — Public contracts are closed sets | The public surface is a contract, and a route added later would be exposed by default if the front door forwarded everything | The public and internal route sets are closed and pinned by a test; the front door forwards the public set only (`REQ-FDR-001`, `002`) |
| IX — Secrets never render | The front door sees `X-API-Key` and `air-token` on every request | It must not log request headers (`REQ-FDR-013`) |

## 5. Requirements

The first column of each row says whose requirement it is: **S** is this service, proved
here; **F** is the front door, proved by air-infrastructure-service.

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-FDR-001` | MUST | **S.** The service's routes are two closed sets. *Public:* `POST /v1/chat`, `POST /v1/query`, `GET` and `DELETE /v1/sessions/{session_id}`. *Internal:* `GET /v1/health`, `GET /v1/ready`, `GET /v1/capabilities`, `GET /metrics`, and the API documentation routes when enabled. A route in the API document that is in neither set fails the build, so a new route cannot become public by default. |
| `REQ-FDR-002` | MUST | **F.** The front door forwards the public set and nothing else. A request for any other path is answered by the front door and never reaches the service. |
| `REQ-FDR-003` | MUST NOT | **S.** The service does not take the caller's identity, tenant, channel, customer or session ownership from any header a proxy may set or a client may forge (`X-Forwarded-For`, `X-Forwarded-Host`, `X-Forwarded-Proto`, `Forwarded`, `X-Real-IP`, `Via`). Identity comes from `X-API-Key` and `air-token` alone, and the same request answers the same with or without those headers. |
| `REQ-FDR-004` | MUST | **S.** A well-formed `X-Request-ID` arriving through the front door is kept and echoed; a missing or malformed one is replaced with a fresh id. The front door passes the header through unchanged, so one id follows a turn from the caller's log into every downstream. |
| `REQ-FDR-005` | MUST | **S.** A streamed response declares that it must not be buffered or cached (`Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Content-Type: text/event-stream`). |
| `REQ-FDR-006` | MUST | **S.** `GET /v1/health` answers 200 with no credential and with every downstream unreachable, so it can be a health-check target. |
| `REQ-FDR-007` | MUST | **F.** The front door's health check is `GET /v1/health`. It never uses `/v1/ready`. |
| `REQ-FDR-008` | MUST | **S.** A request body over `app.max_body_bytes` is refused by the service itself, whatever forwarded it. The front door may refuse it earlier; the service does not rely on that. |
| `REQ-FDR-009` | MUST NOT | **S.** The service does not answer a cross-origin preflight or send an `Access-Control-Allow-*` header. Browsers are not clients. |
| `REQ-FDR-010` | MUST | **F.** The front door delivers `text/event-stream` responses as they are written, without buffering, compression that buffers, or caching. Its response timeout is longer than the turn deadline ceiling (`turn.deadline_ms`), with room for the answer to finish: at least four times it. |
| `REQ-FDR-011` | MUST | **F.** The front door terminates TLS. Its address is private: reachable from the shop's backend network, not from the internet. Inside the cluster the hop to the service is plain HTTP, admitted by the service's network policy from the front door's addresses only. |
| `REQ-FDR-012` | MUST | **F.** The front door limits the request rate per caller and answers 429 beyond it, until this service enforces its own limit. |
| `REQ-FDR-013` | MUST NOT | **F.** The front door does not record `X-API-Key` or `air-token` in its access logs. |

## 6. Acceptance criteria

**AC-1 — A forged proxy header changes nothing** (`REQ-FDR-003`)
- **Given** a customer-channel key
- **When** the same turn is sent once plainly and once with `X-Forwarded-For`, `Forwarded`, `X-Forwarded-Host`, `X-Real-IP` and `Via` naming another party
- **Then** both answer 200 on the customer channel for the key's tenant, and the same headers on a request with no key still answer 401

**AC-2 — A new route is a decision** (`REQ-FDR-001`)
- **Given** the application as built
- **When** its route table is compared with the public and internal sets
- **Then** they are equal; adding a route without adding it to one set fails

**AC-3 — No preflight** (`REQ-FDR-009`)
- **Given** an `OPTIONS /v1/chat` with `Origin` and `Access-Control-Request-Method`
- **When** it reaches the service
- **Then** it is not a 2xx and carries no `Access-Control-Allow-Origin`

**AC-4 — A stream arrives as it is written** (`REQ-FDR-010`)
- **Given** the deployed front door
- **When** a turn is requested with `Accept: text/event-stream`
- **Then** `turn.start` reaches the caller before `turn.end` was written, measurably apart

**AC-5 — Only the public set gets through** (`REQ-FDR-002`)
- **Given** the deployed front door
- **When** `/metrics`, `/v1/ready` and `/v1/capabilities` are requested through it
- **Then** each is a 404 from the front door, and the service's access log shows no such request

**AC-6 — The limit holds** (`REQ-FDR-012`)
- **Given** the deployed front door and its configured rate
- **When** one caller exceeds it
- **Then** the excess is answered 429 by the front door

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| air-model-gateway is down | The service stays in rotation (liveness is unaffected); turns answer degraded, as spec 0003 says | degraded |
| The service is restarting (one replica, `Recreate`) | The front door has no healthy backend and answers 503 until liveness passes; sessions in memory are gone (spec 0015) | none: the caller sees the front door's 503 |
| A turn outlives the front door's timeout | The front door closes the response; the caller sees a cut stream or a 504, and the service finishes or abandons the turn at its own deadline | none delivered |
| A caller exceeds the rate | 429 from the front door; the service never sees the request | none |

## 8. Observability

The service's access log line carries `request_id`, so a request the front door forwarded
is matched to the front door's own log by that id. The front door's logs and its 429 and
5xx counts are the platform's (air-infrastructure-service runbook §7). Nothing new is
measured by this service.

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| 1 | The rate per caller, and whether "caller" is the source address or the API key. The deployment ships a default and makes both settable. | Tuning `REQ-FDR-012` for production | Product owner, DevOps |
| 2 | The hostname per environment, and who issues its certificate. | A first UAT deployment | DevOps |

## 10. Traceability

| Id | Proof |
| --- | --- |
| `REQ-FDR-001` | `tests/unit/test_front_door.py::test_every_route_is_declared_public_or_internal` |
| `REQ-FDR-002` | DEFERRED — the front door's: air-infrastructure-service's `make gke-validate` asserts the rendered route carries exactly the public paths, and its UAT smoke test asks for an internal path through the front door (AC-5) |
| `REQ-FDR-003` | `tests/unit/test_front_door.py::test_proxy_headers_never_change_who_the_caller_is` |
| `REQ-FDR-004` | `tests/unit/test_api_errors.py::test_a_supplied_request_id_is_echoed` |
| `REQ-FDR-005` | `tests/unit/test_events.py::test_the_stream_sets_the_no_buffering_header` |
| `REQ-FDR-006` | `tests/unit/test_system_routes.py::test_health_needs_no_auth_and_no_dependencies` |
| `REQ-FDR-007` | DEFERRED — the front door's: `make gke-validate` asserts the rendered health check path |
| `REQ-FDR-008` | `tests/unit/test_api_errors.py::test_the_body_ceiling_is_a_400_malformed_request` |
| `REQ-FDR-009` | `tests/unit/test_front_door.py::test_a_cross_origin_preflight_is_not_answered` |
| `REQ-FDR-010` | DEFERRED — the front door's: `make gke-validate` asserts the rendered timeout against the turn deadline; unbuffered delivery is provable only against the deployed load balancer (AC-4, UAT smoke) |
| `REQ-FDR-011` | DEFERRED — the front door's: an internal address and a TLS listener are asserted in the render; reachability is provable only in UAT |
| `REQ-FDR-012` | DEFERRED — the front door's: the rate rule is validated by Terraform; AC-6 is provable only in UAT |
| `REQ-FDR-013` | DEFERRED — the front door's: a property of the load balancer's logging configuration, checked by review in the runbook |
