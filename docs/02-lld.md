# AIR Platform — Low-Level Design

**Status:** Draft for review · **Companion docs:** [Plan](00-plan.md) · [HLD](01-hld.md)

This document is the implementation contract. Code follows it; where it turns out to be
wrong, the document is amended rather than silently diverged from.

**Shipped so far: Phases 0 and 1.** The full `/v1` surface is live — `/v1/chat`,
`/v1/query`, `/v1/sessions/{id}` and the SSE event contract — served by a scripted **echo
engine** that walks the real pipeline and stubs the work. 109 tests; `make check` is green.
Sections below describe the whole contract; §15 records what is built and what is not.

---

## 1. Repository layout

```
air-platform/
├── pyproject.toml                  # deps, ruff, mypy strict, pytest config
├── Dockerfile                      # multi-stage, non-root runtime
├── docker-compose.yml              # air-platform + a pointer at air-infra's stack
├── Makefile                        # install / dev / run / test / lint / check
├── .env.example                    # every settings key, documented
├── docs/                           # 00-plan · 01-hld · 02-lld
└── src/air_platform/
    ├── __init__.py                 # __version__
    ├── main.py                     # create_app(), lifespan, middleware wiring
    ├── config.py                   # Settings (pydantic-settings), get_settings()
    ├── constants.py                # Channel / Route / Stage / EventType enums
    │
    ├── api/
    │   ├── deps.py                 # authN → Principal (carries channel + tenant)
    │   ├── errors.py               # AppError hierarchy + RFC 9457 handlers
    │   ├── middleware.py           # request-id, access log, body ceiling  (see §11)
    │   ├── sse.py                  # EventStreamResponse: framing, heartbeat, teardown
    │   └── v1/
    │       ├── router.py           # aggregates the v1 routers
    │       ├── chat.py             # POST /v1/chat            (customer channel)
    │       ├── query.py            # POST /v1/query           (business channel)
    │       ├── sessions.py         # GET/DELETE /v1/sessions/{id}
    │       └── system.py           # /v1/health · /v1/ready · /v1/capabilities
    │
    ├── schemas/
    │   ├── common.py               # Principal, RequestContext, Problem, Usage
    │   ├── chat.py                 # ChatRequest/QueryRequest/TurnResult, TurnOptions
    │   ├── events.py               # the SSE event union (§4) — one model per event
    │   ├── session.py              # Session, Turn, PendingProposal
    │   └── capability.py           # Capability, ToolSpec, discovered downstream inventory
    │
    ├── engine/
    │   ├── turn.py                 # TurnEngine.run() — the pipeline in HLD §4
    │   ├── planner.py              # route selection + capability binding
    │   ├── synthesis.py            # prompt assembly → air-infra gateway → answer
    │   └── budget.py               # per-turn deadline + cost ceiling
    │
    ├── guardrails/
    │   ├── base.py                 # Guardrail protocol, GuardrailVerdict, profiles
    │   ├── injection.py            # prompt-injection / jailbreak heuristics (input)
    │   ├── pii.py                  # redaction both directions
    │   ├── policy.py               # channel policy + tool allow-list
    │   ├── grounding.py            # answer is supported by gathered evidence (output)
    │   └── schema.py               # business-channel structured-output validation (output)
    │
    ├── memory/
    │   ├── session.py              # SessionStore protocol; Redis + in-memory backends
    │   └── window.py               # bounded turn window; older context summarised
    │
    ├── cache/
    │   └── semantic.py             # SemanticCache: embed → ANN → eligibility gate
    │
    ├── clients/
    │   ├── base.py                 # DownstreamClient: timeout, retry, breaker, budget
    │   ├── infra.py                # air-infra over httpx: models + brokered stores (§10)
    │   ├── classifier.py  rag.py  tools.py  action.py  recommender.py
    │   └── discovery.py            # /v1/capabilities polling → live capability inventory
    │
    ├── prompts/
    │   ├── registry.py             # versioned, pinned prompt lookup
    │   └── templates/              # the prompts themselves, one file per pinned version
    │
    ├── observability/
    │   ├── logging.py  metrics.py  tracing.py
    │   └── audit.py                # audit sink; immutable log for the business channel
    └── security/
        └── apikeys.py              # salted-sha256 key store → Principal
```

## 2. Configuration (`config.py`)

`Settings` (pydantic-settings), env prefix `AIR_PLATFORM__`, nested delimiter `__` — the same
shape as air-classifier (`AIR_CLASSIFIER__APP__ENV=development`), so `air-client` targets
configure identically across services. Note this deliberately follows **air-classifier**, not
air-infra: air-infra uses a top-level `environment` of `local|staging|prod`, but air-platform
is a service and matching the service convention is what keeps a single `air-client` target
block coherent.

| Group | Keys | Notes |
| --- | --- | --- |
| `app` | `env`, `host`, `port`, `max_body_bytes` | `development`\|`staging`\|`production`\|`test`; drives fail-open/closed. Port **8081** (air-infra port map) |
| `log` | `level`, `json` | structlog renderer |
| `security` | `api_keys`, `hash_salt`, `allow_unauthenticated` | key records carry `channel`, `tenant`, `scopes` |
| `infra` | `base_url`, `token` | air-infra :8080; also the source of Redis/Postgres creds |
| `downstream` | `classifier`, `rag`, `tools`, `action`, `recommender` → `{base_url, api_key, timeout_ms, enabled}` | Each independently disableable; disabled ⇒ capability absent, not an error |
| `turn` | `deadline_ms`, `max_cost_usd`, `max_model_calls`, `window_turns` | Per-turn budget (§7) |
| `guardrails` | `profiles.customer`, `profiles.business` | Which rules run, and in which direction |
| `cache` | `enabled`, `backend`, `similarity_threshold`, `ttl_seconds` | `memory`\|`redis`; default off until Phase 5 |
| `session` | `backend`, `ttl_seconds`, `proposal_ttl_seconds` | `memory`\|`redis` |
| `prompts` | `registry_path`, `pins` | route → pinned prompt version |

`get_settings()` is `lru_cache`d.

## 3. API surface (v1)

All routes require `X-API-Key`; errors are RFC 9457 `application/problem+json`.

| Method | Path | Purpose | Guard |
| --- | --- | --- | --- |
| GET | `/v1/health` | liveness (process up) | none |
| GET | `/v1/ready` | readiness (air-infra reachable; downstreams probed) | none |
| GET | `/v1/capabilities` | version, channel, live routes, discovered tools, cache/guardrail state | authN |
| POST | `/v1/chat` | conversational turn | authN + `channel=customer` |
| POST | `/v1/query` | business query, structured output | authN + `channel=business` |
| GET | `/v1/sessions/{id}` | turn history | authN + session owner |
| DELETE | `/v1/sessions/{id}` | clear session and any pending proposal | authN + session owner |
| GET | `/metrics` | Prometheus | none (bind-scoped) |

**Streaming is content-negotiated.** `Accept: text/event-stream` streams; anything else
returns the terminal `TurnResult` as one JSON body. Same engine, same events — the
non-streaming path collects them. This is what keeps `air-client`, curl, and a batch caller
on the same contract.

**Session ownership** is checked exactly as `shopassist-service` does it: a session belonging
to a different principal 404s identically to one that does not exist, so the response cannot
be used to probe for session ids.

## 4. The SSE event contract

The stream is the API. Framing is `event: <name>` + `data: <json>`, one JSON object per
event, terminated by a blank line. A `: heartbeat` comment every 15 s keeps corporate proxies
from idling the connection out.

| Event | Payload | When |
| --- | --- | --- |
| `turn.start` | `{turn_id, session_id, channel}` | First, always |
| `stage` | `{stage, status, latency_ms, detail?}` | Each pipeline step in HLD §4 completes. `status` ∈ `ok`\|`skipped`\|`degraded`\|`blocked` |
| `route` | `{routes[], reason, capabilities[]}` | Planner has decided |
| `citation` | `{source_id, title, uri?, snippet}` | Per retrieved source actually used |
| `proposal` | `{proposal_id, action, arguments, risk, summary, expires_at}` | A mutation is proposed. **Nothing has executed** |
| `answer.delta` | `{text}` | **Reserved.** Emitted only once air-infra can stream (HLD §5, §9) |
| `answer` | `{text, structured?, grounded, refusal}` | Terminal answer. `structured` on the business channel |
| `usage` | `{model_calls, tokens, cost_usd, cache_hit}` | Before `turn.end` |
| `error` | `{code, detail, retryable}` | A stage failed unrecoverably |
| `turn.end` | `{turn_id, status, latency_ms}` | Last, always — including after `error` |

Three rules make this survivable for clients:

1. **`turn.start` and `turn.end` always bracket a turn**, even a refused or errored one. A
   client can rely on the frame rather than on inferring completion from silence.
2. **Unknown event names must be ignored.** This is what lets `answer.delta` arrive later
   without a contract version bump, and it is stated in the client-facing docs, not assumed.
3. **An error mid-stream does not become an HTTP error.** Headers are already sent by the
   time most stages run, so failure is an `error` event followed by `turn.end`. Only a
   failure *before* the first byte is an RFC 9457 response.

## 5. Core schemas

```python
# common.py
class Principal(BaseModel):
    key_id: str
    channel: Channel            # customer | business — from the key record, never a header
    tenant: str
    scopes: frozenset[str]

# chat.py
class TurnOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deadline_ms: int | None = None       # clamped to settings.turn.deadline_ms
    max_cost_usd: float | None = None    # clamped likewise
    allow_routes: list[Route] | None = None
    use_cache: bool | None = None
    redact_pii: bool | None = None       # business channel only; customer rejects an override
    include_trace: bool = True

class Confirmation(BaseModel):
    proposal_id: str
    approve: bool

class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str | None = None        # absent ⇒ a new session, returned in turn.start
    message: str = Field(min_length=1)
    confirm: Confirmation | None = None  # the ONLY way to execute a proposal (§8)
    options: TurnOptions | None = None

class QueryRequest(BaseModel):            # business channel
    model_config = ConfigDict(extra="forbid")
    session_id: str | None = None
    query: str = Field(min_length=1)
    output_schema: dict[str, Any] | None = None   # JSON Schema; validated in guardrails-out
    options: TurnOptions | None = None

class TurnResult(BaseModel):              # the non-streaming envelope
    turn_id: str
    session_id: str
    answer: str
    structured: dict[str, Any] | None = None
    citations: list[Citation] = []
    proposal: Proposal | None = None
    routes: list[Route] = []
    grounded: bool
    refusal: bool
    degraded: list[str] = []              # downstreams that were unavailable
    usage: Usage
    trace: list[StageRecord] | None = None

# session.py
class PendingProposal(BaseModel):
    proposal_id: str
    action: str
    arguments: dict[str, Any]
    risk: str
    expires_at: datetime
```

`extra="forbid"` throughout, matching air-classifier: a typo'd field is a 400, never a
silently ignored option.

## 6. Turn engine (`engine/turn.py`)

`TurnEngine.run(request, principal) -> AsyncIterator[Event]`. The engine is a **generator**,
not a function that returns a result — streaming is the native shape and the non-streaming
endpoint is the adapter, rather than the reverse.

Ordering is [HLD §4](01-hld.md) exactly. Notes on the parts that are not obvious:

- **The engine holds no state.** Session comes from `SessionStore`, capabilities from
  `discovery`, budget from a per-turn object. Two replicas serving alternating turns of one
  conversation must be indistinguishable (review item 03).
- **Fan-out is `asyncio.gather(..., return_exceptions=True)`** under one deadline. A client
  that raises contributes a `degraded` entry and a `stage` event with `status=degraded`; it
  never propagates.
- **Synthesis sees only what step 7 gathered**, plus the session window. Retrieved content is
  wrapped in explicit delimiters and labelled as data, never concatenated into the
  instruction region — the structural half of injection defence.
- **The budget is checked between stages, not just at the start.** A turn that exhausts its
  cost ceiling stops gathering and synthesises from what it has, emitting `status=degraded`.

## 7. Budget (`engine/budget.py`)

One `TurnBudget` per turn, carrying a monotonic deadline, a remaining-cost figure, and a
model-call count. Every downstream call and every gateway call is issued through it, so the
ceiling is enforced at the one place calls are made rather than by each caller remembering.
Cost is read back from the gateway's own `cost_usd` on each response, so air-platform's
accounting cannot drift from air-infra's.

## 8. Mutations (`clients/action.py`)

The propose → confirm → execute flow from [HLD §6](01-hld.md):

```python
# turn N — the planner selected the action route
proposal = await action.propose(capability, arguments, ctx)   # air-action validates & prices
await session.set_pending(proposal, ttl=settings.session.proposal_ttl_seconds)
yield ProposalEvent(...)          # nothing has changed

# turn N+1 — ONLY via ChatRequest.confirm
pending = await session.get_pending(session_id)
if pending is None or pending.proposal_id != confirm.proposal_id:
    raise ProposalNotFound(...)   # 404 — expired, cancelled, or never existed
if not confirm.approve:
    await session.clear_pending(session_id); ...
result = await action.execute(
    pending, idempotency_key=f"{session_id}:{pending.proposal_id}", ctx=ctx
)
```

Three invariants, each enforced in code rather than by prompt:

1. **`ChatRequest.confirm` is the only path to `action.execute`.** No prose, in any language
   or phrasing, reaches it. A model that has been persuaded to agree cannot produce the field.
2. **The idempotency key is derived from the proposal id**, not generated per attempt, so a
   reconnected stream or a client retry cannot double-execute.
3. **Any turn that does not carry a matching `confirm` clears the pending proposal**
   (Plan §4 Q3). A stale "yes" has nothing left to point at.

`clients/tools.py` has no `execute`-shaped method at all. The read/write split is a property
of the module surface, not a runtime check.

## 9. Guardrails (`guardrails/`)

`Guardrail` protocol: `check(payload, ctx) -> GuardrailVerdict{allow, transformed, rule, reason}`.
Profiles are assembled per channel from config; each is an ordered list, and the first block
short-circuits.

| Direction | Rule | customer | business |
| --- | --- | --- | --- |
| in | `injection` — instruction-override and jailbreak patterns | ✅ | ✅ |
| in | `pii` — redact before anything leaves the process | ✅ always | configurable |
| in | `policy` — channel tool allow-list, scope check | ✅ | ✅ |
| out | `grounding` — claims traceable to gathered evidence | ✅ | ✅ |
| out | `pii` — redact model output | ✅ always | configurable |
| out | `schema` — validate against `output_schema` | — | ✅ (review item 08) |

A block is a **clean refusal**: `stage{status=blocked}`, an `answer` with `refusal=true`, and
`turn.end{status=refused}`. Never a 5xx, and never a silent empty answer. Every block logs the
rule that fired, so the false-refusal rate in Plan §6 is measurable.

## 10. Downstream clients (`clients/`)

`DownstreamClient` base: one `httpx.AsyncClient` per service, timeout from config, bounded
retry on idempotent calls only, a circuit breaker, and the `TurnBudget` deadline applied as
the effective timeout. Request id and tenant propagate on every call.

The guard/breaker/bulkhead primitives are **lifted from air-classifier's
[`resilience/`](../../air-classifier/src/air_classifier/resilience/)** (`guard.py`,
`breaker.py`, `bulkhead.py`) rather than rewritten — fan-out here has the same failure modes
its tier ladder does, and a second implementation would drift.

**air-infra is a service dependency, not a package one.** `clients/infra.py` speaks to the
gateway over `httpx` rather than importing `air_infra_client`. That follows air-classifier's
precedent — its [`providers/infra_provider.py`](../../air-classifier/src/air_classifier/providers/infra_provider.py)
does the same — and avoids a build-time coupling between the two repos that a path dependency
would create, given the SDK is not published anywhere. The contract is air-infra's published
API; `httpx` is the transport. The trade is real: a change to air-infra's request shape is not
caught by a type checker here, so the contract is pinned by tests instead.

`discovery.py` polls each service's `/v1/capabilities` on an interval and caches the result.
The planner binds against **this live inventory**, which is what makes "a new tool in
air-tools needs no release here" true, and what makes a dead service a missing capability
rather than a failed turn.

Every client ships a **fixture-backed fake** used by the tests, so Phases 3–4 are buildable
against services that are still empty repos (Plan §6).

## 11. Middleware

`RequestContextMiddleware` → `AccessLogMiddleware` → `BodySizeLimitMiddleware`, as raw ASGI
middleware, following air-classifier's
[`api/middleware.py`](../../air-classifier/src/air_classifier/api/middleware.py) — same
reasoning applies here and the implementation is worth lifting rather than rewriting.

One addition it does not have: the access log must record a **streaming** response's true
outcome. Status 200 is sent before the turn runs, so `turn.end{status}` is the real result;
the access log records both, and the request metric is labelled on the turn status rather
than the HTTP status.

## 12. Errors (RFC 9457)

`AppError(status, code, detail)` subclasses — `Unauthorized(401)`, `Forbidden(403)`,
`SessionNotFound(404)`, `ProposalNotFound(404)`, `MalformedRequest(400)`, `RateLimited(429)`,
`TurnBudgetExceeded(429)`, `DependencyUnavailable(503)` — rendering to
`application/problem+json` with `request_id`, exactly as air-infra and air-classifier do.

`DependencyUnavailable` is reserved for **air-infra being unreachable**. Every other
downstream is a degradation, not an error (HLD §7).

## 13. Observability

- **Logs** — structlog; JSON in prod. `request_id`, `turn_id`, `tenant`, `channel`, `key_id`
  bound per request and cleared in a `finally`.
- **Trace** — one OTel trace per turn, with a span per stage and per downstream call, so
  review item 05's "gateway → router → RAG → tools → LLM → stream" is a single readable trace.
- **Metrics** — `/metrics`:
  `air_platform_turns_total{channel,status,route}`,
  `air_platform_stage_seconds{stage}`,
  `air_platform_turn_cost_usd_total{channel,tenant}`,
  `air_platform_cache_lookups_total{result}`,
  `air_platform_guardrail_blocks_total{direction,rule,channel}`,
  `air_platform_downstream_calls_total{service,outcome}`,
  `air_platform_proposals_total{outcome}`.
- **Audit** — `turn.completed`, `proposal.created`, `action.executed`, `guardrail.blocked`.
  The business channel additionally writes to an append-only sink (review item 05); the sink
  is a backend swap, log-only in v1.

## 14. Testing

- `tests/test_system.py` — health/ready/capabilities over `httpx.ASGITransport`, no network.
- `tests/test_events.py` — the §4 contract: bracketing, ordering, unknown-event tolerance,
  and that an error mid-stream is an event rather than an HTTP status.
- `tests/test_engine.py` — the pipeline against fake downstreams: cache hit short-circuits,
  each downstream killed in turn still answers, budget exhaustion degrades.
- `tests/test_mutations.py` — the §8 invariants. **Explicitly: prose that reads as consent
  does not execute; a mismatched or expired `proposal_id` 404s; a replayed execute is
  idempotent.**
- `tests/test_guardrails.py` — an injection corpus in, a refusal out; per-channel profiles.
- `tests/test_isolation.py` — tenant A never reads tenant B's session or cache entry.
- `tests/test_channels.py` — a customer key cannot reach `/v1/query`, and `redact_pii=false`
  is rejected on the customer channel.

## 15. Build status

| Phase | State |
| --- | --- |
| 0 — Skeleton | **Shipped.** config · auth (channel + tenant on the key record) · middleware · RFC 9457 errors · `/v1/health` `/v1/ready` `/v1/capabilities` · metrics · air-infra probe · Dockerfile · compose · Makefile |
| 1 — Contracts | **Shipped.** `/v1/chat` · `/v1/query` · `/v1/sessions/{id}` · the §4 event contract (`api/sse.py`) · every §5 schema · session store with tenant-namespaced keys · the propose → confirm → execute gate · **echo engine** (`engine/echo.py`) |
| 2 — Orchestrator core + guardrails | Not started. Replaces `EchoEngine._answer` and turns the skipped stages real; **the event contract does not change** |
| 3 — Read path | Not started. **Blocked on §16's tool-calling question** for the native-tool-calling variant |
| 4 — Write path | Not started |
| 5 — Semantic cache + business channel | Not started |
| 6 — Evaluation, hardening & ops | Not started |

### What the echo engine is for

`engine/echo.py` exists so a client can be integrated against the **real** contract before
the orchestrator is written. It walks the §4 pipeline in order, emits the true event
sequence, and keeps real session state — then echoes instead of calling air-classifier,
air-rag and the gateway.

Stages it cannot really run report `skipped` with a reason, never `ok`. That is deliberate:
a client must be able to tell a stubbed turn from a real one, and this engine must never be
mistaken for working software in a demo. Sending `/propose` in a message exercises the
mutation path — a literal trigger, because a stub must not appear to understand intent it
does not.

### Claims proved by tests, not by assertion

- **A fresh checkout with nothing else running boots, serves, and reports itself unready** —
  air-infra down is a 503 on `/v1/ready` with a named cause, not a crash loop.
- **Two keys in one process describe the service differently.** `/v1/capabilities` returns a
  different channel and guardrail profile per credential, and no header or query parameter
  can change which one you get (`test_channels.py`).
- **Prose that reads as consent executes nothing.** Only the structured `confirm` field can,
  a proposal is single-use, and any unrelated turn cancels it (`test_turns.py`).
- **`turn.start` and `turn.end` bracket every turn**, a mid-stream failure is an `error`
  event rather than an HTTP status, and `answer.delta` is defined but never emitted
  (`test_events.py`).

## 16. Marked TODOs (fill-in points for later phases)

- `cache/semantic.py` — real ANN lookup and the eligibility gate (Phase 5; Plan §4 Q5).
- `memory/window.py` — summarisation of older turns (Phase 2 ships truncation).
- `guardrails/injection.py` — heuristics ship first; a model-based classifier is Phase 6.
- `observability/audit.py` — durable append-only sink for the business channel (Phase 5).
- `engine/planner.py` — **native tool-calling, blocked on air-infra ([HLD §9](01-hld.md)).**
  Ships as schema-constrained JSON decomposition using the gateway's existing `json_schema`
  support, behind the same `Planner` interface, so the migration is an implementation swap.
- `api/sse.py` — `answer.delta` emission, blocked on air-infra streaming (HLD §5, §9).
