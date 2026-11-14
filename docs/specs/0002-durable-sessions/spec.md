# 0002 — Durable sessions (Redis behind `SessionStore`)

**Status:** Draft · **Area:** `SESS` · **Phase:** 2b · **Updated:** 2026-09-12
**Derives from:** [Plan §5 Phase 2b](../../00-plan.md) · [LLD §16](../../02-lld.md) · G6
**Supersedes:** —

> Forward spec — written before the code. Contrast with [0001](../0001-mutation-gate/spec.md),
> which was reverse-engineered from shipped behaviour.

## 1. Problem

Conversation state lives in a `dict` in one process. Everything about the *shape* is already
right — the key is `{key_prefix}{tenant}:session:{session_id}`, tenant first, precisely so a
durable backend inherits it — but the storage is not.

The consequence is not that the service breaks. It is worse: it appears to work. Every replica
answers every request, and a conversation silently forgets itself whenever the load balancer
moves it or a deploy restarts a pod. There is no error, no metric, and no log line at the
moment the history is lost.

`build_session_store` currently accepts `backend: redis`, logs a warning, and returns the
in-memory store anyway.

## 2. Outcome

With `session.backend = redis`, conversation state survives process restart and is shared
across replicas. Two replicas serving alternating turns of one conversation are
indistinguishable from one replica serving both.

Selecting `redis` and getting memory becomes impossible: a misconfiguration fails at startup,
and a Redis outage degrades the turn honestly rather than silently dropping history.

## 3. Scope

**In scope**

- A `RedisSessionStore` implementing the existing `SessionStore` protocol.
- Atomic consumption of a pending proposal — resolving [0001 Q2](../0001-mutation-gate/spec.md).
- Credential acquisition through air-infra's broker (G6).
- Serialisation of `Session` with a version tag, so a rolling deploy can read both shapes.
- Behaviour when Redis is unreachable.

**Out of scope**

- **The semantic cache**, which is also Redis-backed and also tenant-namespaced. Phase 5, and
  a different eligibility problem — sharing a backend does not make it the same spec.
- **Changing the `SessionStore` protocol.** If this spec needs a new method, that is a signal
  the protocol was wrong, and it becomes its own change.
- **Session migration.** In-memory sessions are lost on deploy today and will be lost on the
  deploy that ships this. Nothing to migrate.

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| IV — Tenant-namespaced | The whole point | Key shape is unchanged; `REQ-SESS-003` pins it |
| III — Missing downstream narrows | Redis is a new failure mode on the turn path | `REQ-SESS-007` degrades the turn; but see Q1 — a session store is not obviously a "capability" that can be dropped |
| II — Mutations proposed | Proposal state moves to Redis | `REQ-SESS-006` makes consumption atomic, strengthening `REQ-MUT-006` |
| VIII — No singletons | The client is a new collaborator | Constructed in `create_app`, held on `AppState`, closed on lifespan shutdown |
| IX — Secrets never render | Brokered credentials | `REQ-SESS-011` |

## 5. Requirements

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-SESS-001` | MUST | With `backend: redis`, a session written by one process is readable by another after the first has exited. |
| `REQ-SESS-002` | MUST | Two replicas serving alternating turns of one conversation produce the same result as one replica serving all of them. |
| `REQ-SESS-003` | MUST | The key remains exactly `{key_prefix}{tenant}:session:{session_id}`, tenant first, so a scan is tenant-scoped. |
| `REQ-SESS-004` | MUST | `get` returns `None` indistinguishably for absent and not-this-principal's, as the in-memory store does. |
| `REQ-SESS-005` | MUST | Every write sets the key's expiry to `session.ttl_seconds`, so an idle conversation expires without a sweeper. |
| `REQ-SESS-006` | MUST | `take_pending` is atomic: given two concurrent confirmations of one proposal, at most one observes it. |
| `REQ-SESS-007` | MUST NOT | A Redis outage fail the turn. The turn answers without history and reports `degraded`. |
| `REQ-SESS-008` | MUST NOT | Configuring `redis` ever yield the in-memory store. |
| `REQ-SESS-009` | MUST | Redis credentials are obtained from air-infra's broker, not from a static URL, when `infra` is configured. |
| `REQ-SESS-010` | MUST | The stored payload carries a schema version, and a reader rejects a version it does not understand rather than silently misreading it. |
| `REQ-SESS-011` | MUST NOT | A brokered credential appear in any log line, repr or error message. |
| `REQ-SESS-012` | SHOULD | `/v1/ready` reports the session backend's reachability without gating on it — air-model-gateway alone gates readiness (Article III). |

## 6. Acceptance criteria

**AC-1 — Survives restart** (`REQ-SESS-001`)
- **Given** a session with two turns of history, written by store instance A
- **When** A is discarded and a fresh store instance B is constructed against the same Redis
- **Then** B reads the session with both turns intact

**AC-2 — One proposal, two confirmations** (`REQ-SESS-006`)
- **Given** a session with one pending proposal
- **When** two confirmations of the same `proposal_id` are issued concurrently
- **Then** exactly one observes the proposal and the other observes `None` — never both

**AC-3 — Outage degrades, does not fail** (`REQ-SESS-007`)
- **Given** `backend: redis` and Redis refusing connections
- **When** a turn is sent to `/v1/chat`
- **Then** the response is 200, the answer is produced, `turn.end` reports `degraded`, and the
  `context` and `persist` stages report `degraded` with a reason

**AC-4 — Misconfiguration fails loudly** (`REQ-SESS-008`)
- **Given** `backend: redis` with an unusable configuration
- **When** the application is constructed
- **Then** construction raises — it does not warn and return an in-memory store

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| Redis unreachable at startup | Service **starts** (Article III); `/v1/ready` reports the backend unreachable but does not 503 on it | — |
| Redis unreachable mid-turn, on read | Turn proceeds with no history; `context` stage `degraded` | `degraded` |
| Redis unreachable mid-turn, on write | Turn answers; `persist` stage `degraded`; the turn is lost from history | `degraded` |
| Payload version unrecognised | Treated as absent, logged once with the key's tenant but not its contents | `degraded` |
| Redis slow | Bounded by a timeout well inside the turn deadline, then treated as unreachable | `degraded` |

A turn that loses its history is materially worse than one that keeps it, so `degraded` is the
honest status even though the user still gets an answer. Article III requires the answer;
it does not require pretending nothing happened.

## 8. Observability

- A counter of session reads and writes by outcome (`ok` · `miss` · `degraded`).
- The `context` and `persist` stage events carry the reason when degraded.
- `/v1/ready` gains a session-backend entry, reported but not gating (`REQ-SESS-012`).
- No key contents and no credential in any log line; tenant is a label, session id is not
  (unbounded cardinality).

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| Q1 | Is a session store a droppable capability under Article III, or is silently losing history worse than refusing the turn? `REQ-SESS-007` assumes droppable. A conversation that forgets itself mid-dialogue may be worse UX than an honest 503. | `REQ-SESS-007` | — |
| Q2 | `session.redis_url` defaults to `redis://localhost:6379/1`, so `_guard_redis_backends` — which only fires on an *empty* URL — can essentially never trigger. Should the default be empty so the guard becomes real? That would make `REQ-SESS-008` enforceable at startup. | `REQ-SESS-008` | — |
| Q3 | Does `REQ-SESS-009` mean brokered credentials replace `redis_url`, or that `redis_url` stays as the development path and broking is used only when `infra.api_key` is set? | `REQ-SESS-009` | — |
| Q4 | Is `REQ-SESS-006` satisfiable with `GETDEL`, or does consuming a proposal held *inside* the session payload require a Lua script or `WATCH`/`MULTI`? This determines whether the proposal moves to its own key. | `REQ-SESS-006` | — |

> Q1 and Q2 must be resolved before this spec moves to `Accepted` — both change what gets
> built. Q3 and Q4 are implementation questions and belong in `plan.md`.

## 10. Traceability

> Forward spec: no requirement is proved yet. Every row is `DEFERRED` until the tasks in
> `tasks.md` land, and each names the test that will prove it so the names are decided before
> the code is written.

| Id | Proof |
| --- | --- |
| `REQ-SESS-001` | DEFERRED — Phase 2b · `tests/unit/test_session_store_redis.py::test_a_session_survives_a_new_store_instance` |
| `REQ-SESS-002` | DEFERRED — Phase 2b · `tests/unit/test_session_store_redis.py::test_two_stores_serve_alternating_turns_identically` |
| `REQ-SESS-003` | DEFERRED — Phase 2b · `tests/unit/test_session_store_redis.py::test_the_key_shape_is_tenant_first` |
| `REQ-SESS-004` | DEFERRED — Phase 2b · `tests/unit/test_session_store_redis.py::test_absent_and_not_yours_are_indistinguishable` |
| `REQ-SESS-005` | DEFERRED — Phase 2b · `tests/unit/test_session_store_redis.py::test_every_write_refreshes_the_ttl` |
| `REQ-SESS-006` | DEFERRED — Phase 2b · `tests/unit/test_session_store_redis.py::test_concurrent_confirmations_consume_one_proposal_once` |
| `REQ-SESS-007` | DEFERRED — Phase 2b · `tests/unit/test_turns.py::test_a_session_backend_outage_degrades_rather_than_fails` |
| `REQ-SESS-008` | DEFERRED — Phase 2b · `tests/unit/test_config.py::test_selecting_redis_never_yields_memory` |
| `REQ-SESS-009` | DEFERRED — Phase 2b · `tests/unit/test_infra_client.py::test_redis_credentials_come_from_the_broker` |
| `REQ-SESS-010` | DEFERRED — Phase 2b · `tests/unit/test_session_store_redis.py::test_an_unknown_payload_version_reads_as_absent` |
| `REQ-SESS-011` | DEFERRED — Phase 2b · `tests/unit/test_session_store_redis.py::test_no_credential_reaches_a_log_line` |
| `REQ-SESS-012` | DEFERRED — Phase 2b · `tests/unit/test_system_routes.py::test_ready_reports_the_session_backend_without_gating_on_it` |
