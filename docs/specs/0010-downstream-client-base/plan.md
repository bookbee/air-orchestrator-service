# 0010 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Implemented · **Updated:** 2026-11-14

## 1. Approach

**`clients/base.py`.** `DownstreamClient(service, config)` owns the lazily built
`httpx.AsyncClient` with `X-API-Key`, and `bound_s()` (timeout, `deadline_share`,
remaining). Its one call method is `post_json(path, body, *, timeout_s, parse)`. That method
adds `X-Request-ID`, makes one attempt, and runs `parse` on the JSON inside the breaker's
verdict, so a body that fails validation counts like a 500. Every failure is raised as
`DownstreamError(reason, status)`, with a message built from the service name and status
code only. A subclass overrides `_error()` to narrow the exception type (`ClassifierError`,
`RagError`), so the engine catches one service's failures without catching another's.

**`CircuitBreaker`** is lifted from air-classifier's `resilience/breaker.py`, less the
bulkhead and the multi-probe half-open. It has no lock: every transition runs between
awaits on one event loop. The clock is injectable, so tests step over the cooldown rather
than sleep through it. A `BaseException` escaping a call (cancellation) releases a probe
slot and records nothing.

**`/v1/capabilities`** gathers `probe()` from `AppState.optional_clients()` concurrently.
`probe()` reports an open breaker without a request, and otherwise sends `GET /v1/health`
on `health_timeout_ms`. A probe never moves the breaker. A route whose service is
unreachable drops out of `routes`. `/v1/ready` is unchanged.

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `clients/base.py` | new | `DownstreamClient`, `DownstreamError`, `CircuitBreaker` |
| `clients/classifier.py` | modified | subclasses `DownstreamClient`; `ClassifierError` subclasses `DownstreamError` |
| `config.py` | modified | `ServiceSettings`: `deadline_share`, `health_timeout_ms`, `breaker_threshold`, `breaker_cooldown_s` |
| `api/deps.py`, `main.py` | modified | `AppState.optional_clients()`; lifespan closes each |
| `api/v1/system.py`, `schemas/common.py` | modified | live status on `/v1/capabilities`; `CapabilitiesResponse.downstreams` |

## 3. Contract impact

- [ ] No change to public contracts
- [x] Additive only: `CapabilitiesResponse.downstreams`. A `CLASSIFY` stage can now say
  `air-classifier circuit is open`
- [ ] Breaking

## 4. Configuration

Per optional service: `HEALTH_TIMEOUT_MS` (2000), `BREAKER_THRESHOLD` (5),
`BREAKER_COOLDOWN_S` (30). `DEADLINE_SHARE` moves from the classifier's settings to every
service's, with the same default (0.25) for the classifier.

## 5. Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Lift air-classifier's whole `guard()` (bulkhead, retry, breaker) | Retries spend the turn's deadline share twice; a bulkhead needs concurrency limits nobody has measured here |
| Probe optional services from `/v1/ready` | Spec 0005 `REQ-CLS-010`, Article III |
| Let a probe close or open the breaker | A health endpoint answering says little about the real call path under load |

## 6. Test strategy

`tests/unit/test_client_base.py` against a respx service and a fake clock. The
classifier's existing tests prove the refactor changed no behaviour. One test in
`test_classify_stage.py` drives the breaker through real turns, and two in
`test_system_routes.py` cover `/v1/capabilities` and `/v1/ready`.
