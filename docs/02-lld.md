# AIR Orchestrator Service — Low-Level Design

**Status:** Baseline · **Last reviewed:** 2026-09-06 · **Companion docs:** [Plan](00-plan.md) · [HLD](01-hld.md)

This document is the implementation contract. Code follows it; where it turns out to be
wrong, the document is amended rather than silently diverged from.

**Shipped so far: Phases 0, 1 and 2a.** The full `/v1` surface is live — `/v1/chat`,
`/v1/query`, `/v1/sessions/{id}` and the SSE event contract — served by a real turn engine:
guardrails both ways, a versioned prompt registry, synthesis through air-llm, and enforced
turn and session budgets. Classification, downstream fan-out and the semantic cache are not
built, and the stages that would run them report `skipped` with a reason rather than `ok`.
178 tests; `make check` is green.

**Read §15 first.** Sections 1-14 describe the whole contract, including parts that are
designed but unbuilt. §15 is the authoritative record of what exists today, and every entry
in the layout below is marked accordingly.

---

## 1. Repository layout

Modules marked **○** are designed here but **not built yet** — they are the fill-in points for
the phases in §15, not missing files. Everything unmarked exists today.

```
air-orchestrator-service/
├── pyproject.toml                  # deps, ruff, mypy strict, pytest config
├── Dockerfile                      # multi-stage, non-root runtime
├── docker-compose.yml              # this service + a pointer at air-infra's stack
├── Makefile                        # install / dev / run / test / lint / check
├── .env.example                    # every settings key, documented
├── CLAUDE.md                       # orientation for AI coding agents
├── docs/                           # 00-plan · 01-hld · 02-lld · openapi.json
└── src/air_orchestrator_service/
    ├── main.py                     # create_app(), lifespan, middleware wiring
    ├── config.py                   # Settings (pydantic-settings), get_settings()
    ├── constants.py                # Channel / Route / Stage / EventType enums
    │
    ├── api/
    │   ├── deps.py                 # authN → Principal (carries channel + tenant)
    │   ├── errors.py               # AppError hierarchy + RFC 9457 handlers
    │   ├── middleware.py           # request-id, access log, body ceiling  (§11)
    │   ├── sse.py                  # SSE framing, heartbeat, teardown
    │   └── v1/
    │       ├── router.py           # aggregates the v1 routers
    │       ├── chat.py             # POST /v1/chat            (customer channel)
    │       ├── query.py            # POST /v1/query           (business channel)
    │       ├── sessions.py         # GET/DELETE /v1/sessions/{id}
    │       └── system.py           # /v1/health · /v1/ready · /v1/capabilities · /metrics
    │
    ├── schemas/
    │   ├── common.py               # Principal, DependencyStatus, Usage
    │   ├── chat.py                 # ChatRequest/QueryRequest/TurnResult, TurnOptions
    │   ├── events.py               # the SSE event union (§4) — one model per event
    │   ├── errors.py               # ProblemDetail (RFC 9457)
    │   ├── session.py              # Session, Turn, PendingProposal
    │   └── capability.py         ○ # discovered downstream inventory       (Phase 3)
    │
    ├── engine/
    │   ├── turn.py                 # TurnEngine.run() — the pipeline in HLD §4.
    │   │                           #   Budget (§7) is enforced inline here today
    │   ├── planner.py            ○ # route selection + capability binding  (Phase 3)
    │   └── budget.py             ○ # extracted TurnBudget, once fan-out needs it
    │
    ├── guardrails/
    │   ├── boundary.py             # delimit untrusted content before it enters a prompt
    │   ├── injection.py            # prompt-injection / jailbreak heuristics (input)
    │   ├── scope.py                # abuse · discount negotiation · competitor mentions
    │   ├── escalation.py           # explicit request to reach a human
    │   ├── pii.py                  # masking in, scanning out
    │   ├── grounding.py          ○ # answer supported by gathered evidence  (Phase 3)
    │   └── schema.py             ○ # business-channel output validation     (Phase 5)
    │
    ├── memory/
    │   ├── session.py              # SessionStore protocol + in-memory backend.
    │   │                           #   Redis backend is Phase 2b, same protocol
    │   └── window.py             ○ # summarise older turns  (truncation ships first)
    │
    ├── cache/                    ○ # semantic.py: embed → ANN → eligibility gate (Phase 5)
    │
    ├── clients/
    │   ├── llm.py                  # air-llm — the only model path (G7)
    │   ├── infra.py                # air-infra — brokered stores and secrets
    │   ├── base.py               ○ # shared timeout/retry/breaker/budget     (Phase 3)
    │   ├── classifier.py rag.py tools.py action.py recommender.py  ○   (Phases 3-4)
    │   └── discovery.py          ○ # /v1/capabilities polling → live inventory (Phase 3)
    │
    ├── prompts/
    │   └── registry.py             # versioned, pinned prompt lookup; built-in defaults
    │
    ├── observability/
    │   ├── logging.py              # structlog + per-request context binding
    │   ├── metrics.py              # Prometheus, on a private registry
    │   ├── tracing.py            ○ # OTel spans per stage and downstream call (Phase 6)
    │   └── audit.py              ○ # append-only sink for the business channel (Phase 5)
    │
    └── security/
        └── api_keys.py             # salted-sha256 key store → Principal
```

## 2. Configuration (`config.py`)

`Settings` (pydantic-settings), env prefix `AIR_ORCHESTRATOR_SERVICE__`, nested delimiter
`__` — the same *shape* as air-classifier (`AIR_CLASSIFIER__APP__ENV=development`), with the
prefix derived from this repo's own name, so `air-client` targets configure identically across
services. Note this deliberately follows **air-classifier**, not air-infra: air-infra uses a
top-level `environment` of `local|staging|prod`, but this is a service, and matching the
service convention is what keeps a single `air-client` target block coherent.

The package, env prefix and metric namespace all spell the repo name in full
(`air_orchestrator_service`, `AIR_ORCHESTRATOR_SERVICE__`, `air_orchestrator_service_*`).

The estate is mid-migration on this point. `air-classifier` already ships an
`air_classifier_service` package but kept `AIR_CLASSIFIER__` and `air_classifier_*` for its
env prefix and metrics; `air-rag`, `air-tools`, `air-llm` and `air-infra` are still
`air_<name>` throughout. This repo's package name follows air-classifier; its prefix and
metric namespace are spelled in full. Both are deliberate, and worth revisiting only as one
estate-wide decision rather than per repo.

| Group | Keys | Notes |
| --- | --- | --- |
| `app` | `env`, `host`, `port`, `workers`, `root_path`, `docs_enabled`, `max_body_bytes`, `shutdown_grace_seconds` | `env` is `development`\|`staging`\|`production`\|`test` and drives fail-open/closed. Port **8081** (air-infra port map) |
| `llm` | `base_url`, `api_key`, `timeout_s`, `health_timeout_s`, `max_retries`, `default_model` | air-llm :8083. **Mandatory** — no `enabled` flag, and `/v1/ready` gates on its probe |
| `infra` | `base_url`, `api_key`, `timeout_s`, `health_timeout_s`, `max_retries` | air-infra :8080. Mandatory in shape, unused on the turn path until Phase 2b |
| `downstream` | `classifier`, `rag`, `tools`, `action`, `recommender` → `{enabled, base_url, api_key, timeout_ms}` | Each independently disableable, and **all default to `enabled=false`**: a fresh checkout must not report itself degraded against services that do not exist. Disabled ⇒ capability absent, not an error |
| `turn` | `deadline_ms`, `max_cost_usd`, `max_model_calls`, `window_turns` | Per-turn ceilings (§7) |
| `guardrails` | `profiles.customer`, `profiles.business`, `competitor_names` | Which rules run, and in which direction |
| `cache` | `enabled`, `backend`, `similarity_threshold`, `ttl_seconds` | `memory`\|`redis`; off, and unread, until Phase 5 |
| `session` | `backend`, `ttl_seconds`, `proposal_ttl_seconds`, `max_cost_usd` | `memory`\|`redis`; asking for `redis` today logs a warning and falls back |
| `prompts` | `registry_path`, `pins` | route → pinned prompt version. `registry_path` is reserved: the registry ships built-in defaults and does not load files yet |
| `security` | `api_keys_inline`, `api_keys_file`, `hash_salt`, `allow_unauthenticated`, `anonymous_channel`, `default_rate_limit_rpm`, `log_raw_text` | Key records carry `channel`, `tenant`, `scopes`, `allow_actions` |
| `obs` | `service_name`, `log_format`, `log_level`, `metrics_enabled`, `metrics_path` | |

`get_settings()` is `lru_cache`d, but `create_app(settings)` takes settings as an argument and
never reads the environment itself — so a test drives a fully assembled app without touching
the environment or clearing that cache.

## 3. API surface (v1)

All routes require `X-API-Key`; errors are RFC 9457 `application/problem+json`.

| Method | Path | Purpose | Guard |
| --- | --- | --- | --- |
| GET | `/v1/health` | liveness (process up) | none |
| GET | `/v1/ready` | readiness — gates on **air-llm** alone; air-infra and the optional services are reported but do not affect it | none |
| GET | `/v1/capabilities` | version, channel, live routes, guardrail profile, stream shapes, session/turn ceilings — reported **per principal** | authN + `admin:read` |
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
| `answer.delta` | `{text}` | **Reserved.** Emitted only once air-llm can stream (HLD §5, §9) |
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

- **The engine holds no state across requests.** One `TurnEngine` is built per request; the
  session comes from `SessionStore`, and capabilities will come from `discovery` (○ Phase 3).
  Two replicas serving alternating turns of one conversation must be indistinguishable
  (review item 03) — true by construction today, and actually testable from Phase 2b.
- **Fan-out is `asyncio.gather(..., return_exceptions=True)`** under one deadline (○ Phase 3).
  A client that raises contributes a `degraded` entry and a `stage` event with
  `status=degraded`; it never propagates.
- **Synthesis sees only what step 7 gathered**, plus the session window. Untrusted content is
  wrapped in explicit delimiters and labelled as data by `guardrails/boundary.py`, never
  concatenated into the instruction region — the structural half of injection defence. This
  holds today for the customer message; it extends to retrieved passages and tool results
  unchanged when Phase 3 produces them.
- **The budget is checked between stages, not just at the start.** A turn that exhausts its
  cost ceiling stops gathering and synthesises from what it has, emitting `status=degraded`.

## 7. Budget

**Enforced inline in `engine/turn.py` today; `engine/budget.py` is the extraction point.**
A turn computes its deadline once, decrements it before the synthesis call, and binds it to
that call's own timeout rather than a fixed configured one — and checks it *before* issuing a
call, so an already-exhausted budget skips the call instead of starting it. Per-turn and
per-session cost ceilings are checked the same way.

Cost is read back from air-llm's own `cost_usd` on each response, so this service's accounting
cannot drift from the gateway's.

Extracting a `TurnBudget` object earns its keep once Phase 3 fans out to several services at
once and the ceiling has to be enforced across concurrent calls rather than one sequential
one. Doing it before then would be indirection without a second caller.

## 8. Mutations (`clients/action.py` ○ — Phase 4)

The propose → confirm → execute flow from [HLD §6](01-hld.md). **The gate is built and
tested; the air-action call behind it is not.** `engine/turn.py` proposes, stores the pending
proposal on the session, and executes only against a matching structured `confirm` — with
`/propose` as a literal trigger standing in for a planner decision, so the whole path is
exercisable end to end before air-action exists. The sketch below is the shape the client
takes when it lands:

```python
# turn N — the planner selected the action route
proposal = await action.propose(capability, arguments, ctx)   # air-action validates & prices
await session.set_pending(proposal, ttl=settings.session.proposal_ttl_seconds)
yield ProposalEvent(...)          # nothing has changed

# turn N+1 — ONLY via ChatRequest.confirm
if request.confirm is None:
    await sessions.clear_pending(session)   # any unrelated turn cancels the proposal
    ...                                     # and the turn answers normally
pending = await sessions.take_pending(session, request.confirm.proposal_id)
if pending is None or not request.confirm.approve:
    ...                           # expired, cancelled, mismatched or declined:
                                  # a normal 200 turn that executes nothing
result = await action.execute(
    pending, idempotency_key=f"{session_id}:{pending.proposal_id}", ctx=ctx
)
```

**A stale or mismatched `proposal_id` is not a 404.** The turn answers normally and executes
nothing. Returning an error would confirm to the caller whether a given proposal id ever
existed — the same reasoning that makes a session belonging to another principal 404
identically to one that does not exist (§3). `ProposalNotFoundError` is in the §12 catalogue
but deliberately unraised on this path; `take_pending` is atomic, which is what makes a
proposal single-use against a replayed stream.

Three invariants, each enforced in code rather than by prompt:

1. **`ChatRequest.confirm` is the only path to `action.execute`.** No prose, in any language
   or phrasing, reaches it. A model that has been persuaded to agree cannot produce the field.
2. **The idempotency key is derived from the proposal id**, not generated per attempt, so a
   reconnected stream or a client retry cannot double-execute.
3. **Any turn that does not carry a matching `confirm` clears the pending proposal**
   (Plan §4 Q3). A stale "yes" has nothing left to point at.

`clients/tools.py` will have no `execute`-shaped method at all. The read/write split is a
property of the module surface, not a runtime check.

## 9. Guardrails (`guardrails/`)

Each rule is a small, independently testable function rather than a class behind a protocol —
there is no shared `Guardrail` ABC, because the rules do not have a common signature worth
forcing. Profiles are read per channel from `settings.guardrails`.

| Direction | Rule | Module | customer | business |
| --- | --- | --- | --- | --- |
| in | `injection` — instruction-override and jailbreak patterns | `injection.py` | ✅ | ✅ |
| in | `scope` — abuse, discount negotiation, competitor mentions | `scope.py` | ✅ | ✅ |
| in | `escalation` — an explicit request to reach a human | `escalation.py` | ✅ | ✅ |
| in | `pii` — mask before anything leaves the process | `pii.py` | ✅ always | configurable |
| — | `boundary` — delimit untrusted content entering a prompt | `boundary.py` | ✅ | ✅ |
| out | `pii` — scan model output | `pii.py` | ✅ always | configurable |
| out | `grounding` — claims traceable to gathered evidence | ○ Phase 3 | ✅ | ✅ |
| out | `schema` — validate against `output_schema` | ○ Phase 5 | — | ✅ (review item 08) |

`boundary.py` has no direction because it is not a check: it wraps content that did not
originate in this service's own code so a model reads it as data to describe, never as an
instruction to follow. It is the structural half of injection defence, and it covers content
that only exists *after* a downstream call — which nothing else here looks at again.

The pattern-based rules are deliberately narrow and **fail open on anything ambiguous**: a
frustrated real customer must never be blocked by a pattern meant for the obvious case. A
model-based classifier is Phase 6, and air-classifier's zero-shot topic head is the natural
fit.

A block is a **clean refusal**: `stage{status=blocked}`, an `answer` with `refusal=true`, and
`turn.end{status=refused}`. Never a 5xx, and never a silent empty answer. Every block logs the
rule that fired, so the false-refusal rate in Plan §6 is measurable.

## 10. Downstream clients (`clients/`)

`DownstreamClient` base: one `httpx.AsyncClient` per service, timeout from config, bounded
retry on idempotent calls only, a circuit breaker, and the `TurnBudget` deadline applied as
the effective timeout. Request id and tenant propagate on every call.

**Every AIR service is a service dependency, not a package one.** `clients/llm.py` and
`clients/infra.py` speak HTTP over `httpx` rather than importing a client SDK. That follows
air-classifier's precedent — its [`providers/air_llm_provider.py`](../../air-classifier/src/air_classifier_service/providers/air_llm_provider.py)
does the same — and avoids a build-time coupling between repos that a path dependency would
create, given no SDK is published anywhere. The contract is the other service's published API;
`httpx` is the transport. The trade is real: a change to a downstream's request shape is not
caught by a type checker here, so the contract is pinned by tests instead.

**`clients/llm.py` probes `/v1/ready`, not `/v1/health`.** air-llm's health route is
dependency-free liveness and never reflects whether a provider actually answers; its ready
route is gated on "at least one provider reachable" — the only signal that answers "can a turn
be synthesised right now."

`DownstreamClient` (`clients/base.py`) and the five capability clients are **Phase 3**. The
guard/breaker/bulkhead primitives are to be **lifted from air-classifier's
[`resilience/`](../../air-classifier/src/air_classifier_service/resilience/)** rather than rewritten —
fan-out here has the same failure modes its tier ladder does, and a second implementation
would drift.

`discovery.py` (○ Phase 3) polls each service's `/v1/capabilities` on an interval and caches
the result. The planner binds against **this live inventory**, which is what makes "a new tool
in air-tools needs no release here" true, and what makes a dead service a missing capability
rather than a failed turn. Until it exists, `/v1/capabilities` reports the configuration half
only — `DownstreamSettings.routes()`.

Every client ships a **fixture-backed fake** used by the tests, so Phases 3-4 are buildable
against services that are still empty repos (Plan §6).

## 11. Middleware

`RequestContextMiddleware` → `AccessLogMiddleware` → `BodySizeLimitMiddleware`, as raw ASGI
middleware, following air-classifier's
[`api/middleware.py`](../../air-classifier/src/air_classifier_service/api/middleware.py) — same
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

`DependencyUnavailable` is reserved for **air-llm being unreachable** — the one dependency
without which no answer exists. Every other downstream is a degradation, not an error
(HLD §7). Note that a turn whose synthesis call fails does *not* raise it: the turn degrades
to an honest fallback answer with `turn.end{status=degraded}`, and the 503 belongs to
`/v1/ready`, which takes the replica out of rotation.

## 13. Observability

- **Logs** — structlog; JSON in prod. `request_id`, `turn_id`, `tenant`, `channel`, `key_id`
  bound per request and cleared in a `finally`.
- **Trace** (○ Phase 6) — one OTel trace per turn, with a span per stage and per downstream
  call, so review item 05's "gateway → router → RAG → tools → LLM → stream" is a single
  readable trace. The OTel dependencies ship in the `otel` extra; nothing emits spans yet.
- **Metrics** — on `/metrics`, registered against a **private `CollectorRegistry`** rather than
  the global default, so two AIR services sharing one process in an integration test do not
  collide on a duplicate collector name:

  | Metric | Labels |
  | --- | --- |
  | `air_orchestrator_service_requests_total` | `endpoint`, `status`, `key_id` |
  | `air_orchestrator_service_request_seconds` | `endpoint` |
  | `air_orchestrator_service_turns_total` | `channel`, `status`, `route` |
  | `air_orchestrator_service_turn_seconds` | `channel` |
  | `air_orchestrator_service_stage_seconds` | `stage`, `status` |
  | `air_orchestrator_service_turn_cost_usd_total` | `channel`, `tenant` |
  | `air_orchestrator_service_cache_lookups_total` | `result` |
  | `air_orchestrator_service_guardrail_blocks_total` | `direction`, `rule`, `channel` |
  | `air_orchestrator_service_downstream_calls_total` | `service`, `outcome` |
  | `air_orchestrator_service_proposals_total` | `outcome` |
  | `air_orchestrator_service_escalations_total` | `reason` |

  Every label value comes from a closed `StrEnum` in `constants.py`. That is why those enums
  are closed: an open-ended string here would let Prometheus label cardinality grow without
  bound, and renaming a member breaks dashboards as well as callers.
- **Audit** (○ Phase 5) — `turn.completed`, `proposal.created`, `action.executed`,
  `guardrail.blocked`. The business channel additionally writes to an append-only sink (review
  item 05); the sink is a backend swap, log-only in v1.

## 14. Testing

178 tests, all under `tests/unit/`, all hermetic — `pytest` needs nothing running. The
air-infra and air-llm probes are stubbed **in both directions** by `tests/conftest.py`
(`reachable_*` / `unreachable_*`), and their base URLs point at `.invalid` hosts so a probe
that escapes a stub fails loudly instead of quietly reaching a real service. An earlier
version let the "unreachable" case simply happen, on the reasoning that a fresh checkout has
no gateway running — and those tests duly broke the first time air-infra was started locally.

| File | Covers |
| --- | --- |
| `test_system_routes.py` | health · ready · capabilities over `httpx.ASGITransport`, no network |
| `test_events.py` | the §4 contract: bracketing, ordering, unknown-event tolerance, and that a mid-stream error is an event rather than an HTTP status |
| `test_turns.py` | the pipeline and the §8 mutation invariants — **prose that reads as consent does not execute**, a mismatched or expired `proposal_id` 404s, an unrelated turn cancels a pending proposal, budgets degrade rather than error |
| `test_channels.py` | a customer key cannot reach `/v1/query`, and `redact_pii=false` is rejected on the customer channel |
| `test_guardrails_{injection,scope,pii,boundary,escalation}.py` | one file per rule: a corpus in, the expected verdict out |
| `test_api_keys.py` | the key store: digest matching, channel and scope resolution, refusal to load a malformed record |
| `test_api_errors.py` | every failure renders as RFC 9457, and nothing internal escapes |
| `test_config.py` | settings validation, including the production guards |
| `test_llm_client.py` · `test_infra_client.py` | probe and call error handling, with `respx` |
| `test_session_store.py` | tenant-namespaced keys; absent and not-yours are indistinguishable |
| `test_prompts.py` | pinned-version lookup and the built-in default set |

**Gaps worth closing next.** There is no `test_isolation.py` — tenant isolation is asserted
inside `test_session_store.py` for sessions only, and needs its own suite once a cache and
downstream calls exist to leak through. Nothing yet exercises two replicas serving alternating
turns of one conversation (Phase 2b), because there is only one process to serve them.

## 15. Build status

The authoritative record of what exists. Where this table and any prose above disagree, this
table wins.

| Phase | State |
| --- | --- |
| 0 — Skeleton | **Shipped.** config · auth (channel + tenant on the key record) · middleware · RFC 9457 errors · `/v1/health` `/v1/ready` `/v1/capabilities` · metrics · air-infra and air-llm probes · Dockerfile · compose · Makefile |
| 1 — Contracts | **Shipped.** `/v1/chat` · `/v1/query` · `/v1/sessions/{id}` · the §4 event contract (`api/sse.py`) · every §5 schema · session store with tenant-namespaced keys · the propose → confirm → execute gate |
| 2a — Turn engine | **Shipped.** `engine/turn.py`: guardrails in and out · prompt registry · real synthesis through air-llm · enforced turn deadline and per-turn/per-session cost ceilings · escalation on an explicit request for a human. **The event contract did not change** |
| 2b — Durable sessions | **Not started.** Redis behind the existing `SessionStore` protocol; the key shape is already tenant-namespaced, so this is a backend swap |
| 3 — Read path | **Not started.** Clients for air-classifier/air-rag/air-tools/air-recommender, capability discovery, parallel fan-out, grounding checks. Turns `CLASSIFY` and `GATHER` real |
| 4 — Write path | **Not started.** `clients/action.py` behind the gate that already exists |
| 5 — Semantic cache + business channel | **Not started.** Turns `CACHE` real; adds output-schema validation and the audit sink |
| 6 — Evaluation, hardening & ops | **Not started.** Eval suites, OTel tracing, load test, runbook |

### Stubbed stages announce themselves

`CACHE`, `CLASSIFY` and `GATHER` emit `stage{status=skipped}` with a reason — never `ok`.
That is deliberate and load-bearing: a client must be able to tell a stubbed turn from a real
one, and this service must never be mistaken for a complete one in a demo. `_STUBBED` in
`engine/turn.py` is the single list; deleting an entry there is what "turning a stage on"
means.

Routing is likewise still a stand-in. `/propose` is a **literal trigger**, not intent
detection: with `GATHER` stubbed there is nothing to route to besides a direct answer, and a
stand-in must not appear to understand intent it does not.

### Claims proved by tests, not by assertion

- **A fresh checkout with nothing else running boots, serves, and reports itself unready** —
  air-llm down is a 503 on `/v1/ready` with a named cause, not a crash loop.
- **Two keys in one process describe the service differently.** `/v1/capabilities` returns a
  different channel and guardrail profile per credential, and no header or query parameter can
  change which one you get (`test_channels.py`).
- **Prose that reads as consent executes nothing.** Only the structured `confirm` field can,
  a proposal is single-use, and any unrelated turn cancels it (`test_turns.py`).
- **`turn.start` and `turn.end` bracket every turn**, a mid-stream failure is an `error` event
  rather than an HTTP status, and `answer.delta` is defined but never emitted
  (`test_events.py`).
- **A provider failure degrades rather than errors.** air-llm being slow or down produces an
  honest fallback answer and `turn.end{status=degraded}`, not a 5xx (`test_turns.py`).

## 16. Marked TODOs (fill-in points for later phases)

- `cache/semantic.py` — real ANN lookup and the eligibility gate (Phase 5; Plan §4 Q5).
- `memory/window.py` — summarisation of older turns (Phase 2 ships truncation).
- `guardrails/injection.py` — heuristics ship first; a model-based classifier is Phase 6.
- `observability/audit.py` — durable append-only sink for the business channel (Phase 5).
- `engine/planner.py` — **native tool-calling, wanted from air-llm ([HLD §9](01-hld.md)).**
  Ships as schema-constrained JSON decomposition using air-llm's existing `json_schema`
  support, behind the same `Planner` interface, so the migration is an implementation swap.
- `api/sse.py` — `answer.delta` emission, blocked on air-llm streaming (HLD §5, §9).
- `prompts/registry.py` — file-backed versioned prompts. The registry ships built-in defaults
  so a fresh checkout answers with no prompt files configured; `PromptSettings.registry_path`
  is reserved and not read yet. A file format and a reload story are the real work.
