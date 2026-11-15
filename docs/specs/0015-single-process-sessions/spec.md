# 0015 — One process while sessions are in memory

**Status:** Implemented · **Area:** `SPS` · **Phase:** 2a · **Updated:** 2026-11-15
**Derives from:** LLD §15 (Phase 2b), spec 0002 (durable sessions, Draft)
**Supersedes:** —

## 1. Problem

The service shipped with `app.workers=2` while every session lives in a per-process
`InMemorySessionStore`. Each uvicorn worker holds its own sessions, so a follow-up that
lands on the other worker is a `404 No such session`: SIT 2026-11-15 (air-web-console
OR-012) saw `[404, 200, 404, 200, 200, 404, 404, 200]` for one session over fresh
connections, and a concurrent confirm answered once as executed, once as a fresh turn and
once as a 404. `session.backend=redis` does not help yet: it is not built and falls back
to memory with a warning. Circuit breakers and the tools inventory cache are per process
too.

## 2. Outcome

While the session store is in-process, the service runs exactly one worker, whatever
`app.workers` says, and says so at startup. The shipped default and `.env.example` are one
worker, so a fresh checkout is correct without the warning. Scaling out waits for spec 0002.

## 3. Scope

**In scope**

- The worker count the process actually starts with, and the startup warning.
- The `app.workers` default and `.env.example`.

**Out of scope**

- Durable or shared sessions (spec 0002).
- Several replicas of the container: each is its own process, which a load balancer
  without session affinity splits exactly as two workers do. That is spec 0002's to
  solve, and is noted there.
- Refusing to start instead. A deployment carrying `WORKERS=2` would go from half its
  follow-ups failing to all of them, in a crash loop; reducing the count fixes it.

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| III — A missing downstream narrows the answer | Startup must not fail for settings that can be made safe | Settings still validate; the count is reduced, not refused |
| IV — Every key is tenant-namespaced | Sessions are keyed `(tenant, session_id)` in one store | One process means one store, so the key holds for every request |

## 5. Requirements

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-SPS-001` | MUST | While the session store is in-process (`session.backend=memory`, or `redis` until spec 0002 builds it), the service starts one worker. A configured `app.workers` above 1 is reduced to 1, with one `warning` log line naming the configured count, the count used and the reason. |
| `REQ-SPS-002` | MUST | `app.workers` defaults to 1, and `.env.example` sets it to 1. |

## 6. Acceptance criteria

**AC-1 — Two workers in memory start as one** (`REQ-SPS-001`)
- **Given** `app.workers=2` and `session.backend=memory`
- **When** the service starts
- **Then** uvicorn is started with one worker and the log carries `app.workers_reduced` with `configured=2 used=1`

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| `app.workers>1` with an in-process store | One worker, one warning at startup | unchanged |

## 8. Observability

The `app.workers_reduced` warning at startup, with `configured`, `used` and `reason`.

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |

## 10. Traceability

| Id | Proof |
| --- | --- |
| `REQ-SPS-001` | `tests/unit/test_entrypoint.py::test_an_in_process_session_store_runs_one_worker` |
| `REQ-SPS-002` | `tests/unit/test_entrypoint.py::test_one_worker_is_the_default_and_the_example` |
