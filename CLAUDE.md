# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

`make help` lists every target. Three are directives rather than conveniences:

- **`make dev` first on a fresh checkout.** `make install` installs the package alone; ruff,
  mypy, pytest and respx arrive only with `make dev` (`-e ".[all,dev]"`). The venv is built
  with `python3.12` explicitly, so that interpreter must be on `PATH`.
- **`make check`** (ruff + mypy --strict + pytest) before calling work done.
- **`make openapi`** after any route or schema change — `docs/openapi.json` is generated and
  committed, never hand-edited.
- **`make trace`** (part of `make check`) enforces that every spec requirement names a test
  that exists. It fails on a broken link (untraced, double-traced, or a renamed test) but
  only *reports* a declared `GAP`/`DEFERRED` — renaming a test means updating the spec's
  Traceability table. `make spec NAME=<slug>` scaffolds a new one.

Single test: `.venv/bin/pytest tests/unit/test_turns.py::test_name -q` (`pythonpath=["src"]`
and `asyncio_mode=auto` come from `pyproject.toml`, so no env setup is needed).

The tests are hermetic — no service needs to be running. `make run` needs nothing either;
the service boots and serves `/v1/health` with every downstream down.

To drive it by hand, `make env` writes a gitignored `.env` from the committed `.env.example`,
seeding one development key per channel — `airo_local_customer_key` for `POST /v1/chat`,
`airo_local_business_key` for `POST /v1/query`. Both are bound to the development
`hash_salt` and grant no `allow_actions`. `make up` runs the container instead, but it joins
`air-net`, which **air-infra owns and must already be up**; `make run` on the host is the path
that needs nothing from Docker.

## Naming

Repo and distribution are `air-orchestrator-service`; the Python package, env prefix and
metric namespace all match it literally, as does the `airo_` API-key prefix.

The estate is mid-migration to `<name>-service` repos and `<name>_service` packages:
`air-classifier-service` ships `air_classifier_service`, and `air-rag-service` ships
`air_rag_service` with `AIR_RAG_SERVICE__` / `air_rag_service_*`, spelled in full like ours.
`air-tools`, `air-model-gateway` (`air_model_gateway`) and `air-infra` are still `air_<name>`.

Where we differ from air-classifier-service is the env prefix and metric namespace: it keeps
`AIR_CLASSIFIER__` / `air_classifier_*` (no `_service`), we spell the repo name in full. That
is a deliberate choice, not drift — don't "align" it without asking.

**A repo rename does not rename our wire labels.** `DownstreamService` values (`air-rag`,
`air-classifier`) appear in API responses and as Prometheus label values, so they stay as
they are when a sibling repo is renamed. Changing one is a breaking change made on purpose
(as `air-llm` → `air-model-gateway` was, spec 0003), never as part of a prose rename.

## Architecture

FastAPI service on **:8081**, the conversational front door for the AIR platform. It
orchestrates and owns no capability: models are `air-model-gateway` (:8083, the *only* model path — Article VII),
stores/secrets `air-infra` (:8080), plus classifier/tools/action/recommender/rag on
:8082–:8087. All are reached over HTTP with `httpx`, never imported as packages.

Request path: `main.create_app` → `api/middleware.py` (raw ASGI, not `BaseHTTPMiddleware`,
because responses are long-lived streams) → `api/deps.require_principal` → `api/v1/*` →
`engine/turn.TurnEngine.run` → an `AsyncIterator[Event]` that is either SSE-framed
(`api/sse.py`) or folded into one `TurnResult` (`engine.turn.collect`).

The whole `/v1` surface is seven routes: `POST /v1/chat`, `POST /v1/query`,
`GET`/`DELETE /v1/sessions/{session_id}`, and `GET /v1/health`, `/v1/ready`,
`/v1/capabilities`. Streaming is content-negotiated on `Accept: text/event-stream`; the same
engine serves both shapes.

Four invariants shape most of the code. Breaking any of them is a design change, not a
refactor:

1. **The channel comes from the credential, never a header.** `security/api_keys.py` fixes
   `Channel.CUSTOMER` / `Channel.BUSINESS` on the key record; `/v1/chat` and `/v1/query` are
   two handlers pinned by `require_customer` / `require_business` rather than one that
   branches, so a later edit cannot apply one channel's guardrail profile to the other's
   traffic. Channel decides guardrails, output contract, audit sink, quota, tool allow-list.
2. **Mutations are proposed, never performed.** A turn returns a `Proposal` and changes
   nothing; execution requires a *second* turn carrying the structured `confirm.proposal_id`.
   Prose that reads as consent executes nothing. A stale, mismatched or replayed confirm is
   **not** an error — the turn answers normally and executes nothing, so the response cannot
   be used to probe which proposal ids exist.
3. **A missing downstream narrows the answer; it does not refuse one.** Startup fails only for
   unvalidatable settings or an unparseable key file. air-model-gateway being down is a 503 on
   `/v1/ready` (which gates on it alone) and a `degraded` turn that still answers — not a
   crash loop, and not a 5xx from `/v1/chat`.
4. **Every key is tenant-namespaced.** `memory/session.py` keys on `(tenant, session_id)` even
   in the in-memory store, because that key shape is what the Phase 2b Redis backend inherits.
   Absent-vs-not-yours are deliberately indistinguishable to the caller (both 404).

### The turn engine (`engine/turn.py` — the centre of the service)

Stages run in `constants.Stage` order: `guardrails_in → context → cache → classify → plan →
gather → synthesise → guardrails_out → persist`. Each emits a `stage` event with a
`StageStatus`. **A stage this build cannot really run reports `skipped` with a reason, never
`ok`** — `_STUBBED` is the single list, and deleting an entry there is what "turning a stage
on" means. `blocked` (a guardrail refusing) is deliberately distinct from `degraded` (a
downstream coping) so a refusal spike does not look like an outage.

Real today: guardrails both ways, per-channel prompts (spec 0006), synthesis via `ModelGatewayClient.generate`
(JSON callers) or `.stream` (SSE callers, as `answer.delta` through `pii.StreamRedactor` —
spec 0004), `CLASSIFY` via air-classifier when configured (customer channel, fail-soft — spec
0005), `GATHER` via air-rag-service when configured (customer channel: passages screened, numbered and
delimited into the prompt, cited answers emitted as `citation` events — spec 0011), per-turn
and per-session cost ceilings, and a turn deadline that bounds every downstream call. Every
optional client builds on `clients/base.py` (one attempt, four failure reasons, a
per-service circuit breaker — spec 0010). Stubbed: `CACHE` (Phase 5). Routing is still a rule
(customer → `rag` when configured) plus the literal `/propose` trigger — a stand-in must not
appear to understand intent it does not.

### Wiring (`main.py` + `api/deps.py`)

Nothing is a module-level singleton. `create_app` assembles one frozen `AppState` dataclass —
settings, `ApiKeyStore`, `InfraClient`, `ModelGatewayClient`, the session store, and a
`ClassifierClient` and a `RagClient`, each `None` unless configured — and hangs it on
`app.state.air_state` (`deps.STATE_ATTR`). Routes reach it via `Depends(get_app_state)` and
take settings from `get_settings_dep`, never the `get_settings()` process singleton. It is a
dataclass rather than loose `app.state` attributes so a misspelled field fails at import
instead of at request time, and it is what lets a test swap a single collaborator
(`state.model_gateway.generate`, `state.infra.probe`) on an otherwise entirely real app.

### Guardrails

Five independent pure-function modules — `injection`, `scope`, `pii`, `boundary`, `escalation`
— called directly by the engine rather than through a plugin framework. Strictness is per
channel, from `GuardrailSettings.customer` / `.business`, which is what invariant 1 protects.
`boundary.delimit` demarcates anything that did not originate in this service's own code
before it reaches a prompt, and the prompt version used is logged with the turn.

### Prompts (spec 0006)

Each channel synthesises with its own system prompt — `customer_care` on `/v1/chat`,
`business_analyst` on `/v1/query` — picked from `Principal.channel` (`prompts.prompt_for`),
never from the request. A persona cannot be set per message: customer text arrives inside
`boundary.delimit` as data. Brand and assistant names are settings
(`prompts.persona.*`); the rules stay in `prompts/registry.py`. A prompt change is a new
version, and **`make prompt-review`** (live, paid, not in `make check`) runs the multi-turn
review set in `evals/prompts/` before and after it — read the transcripts, not just the flags.

### Contracts that are public surface

- **`constants.py`** — `Stage`, `Route`, `Channel`, `EventType`, `TurnStatus`, scopes. These
  appear in API responses *and* Prometheus label values, so they are closed sets and renaming
  a member is a breaking change for callers and dashboards alike.
- **`schemas/events.py`** — the SSE models *are* the contract. `turn.start`/`turn.end` bracket
  every turn including refused and errored ones; clients must ignore unknown event names (that
  is what let `answer.delta` start arriving without a version bump). Deltas are *provisional*:
  the `answer` event is authoritative and replaces them on a refusal, failure or deadline.
- **`api/errors.py`** — every failure leaves as RFC 9457 `application/problem+json`; nothing
  internal escapes (the catch-all logs the traceback and returns a fixed 500). A failure
  *after* headers are sent is an `error` **event**, not a status — that module handles only
  pre-first-byte failures.

### Configuration

`config.py` is nested pydantic-settings with prefix `AIR_ORCHESTRATOR_SERVICE__` and `__` as
delimiter. `create_app(settings)` takes settings as an
argument and never reads the environment itself — tests assemble a full app with bespoke
settings rather than mutating the environment or clearing `get_settings`' cache. Budgets and
guardrail profiles live in settings so tuning cost/latency never requires a deploy. Every
optional downstream defaults to `enabled=false`: a fresh checkout must not report itself
degraded against services that do not exist.

## Conventions

- **Structured logging via `structlog` (`get_logger`), never `print`.** The lint and type
  settings live in `pyproject.toml`; the long package name puts multi-name imports over the
  line limit — let `make fmt` wrap them.
- **Module docstrings carry the rationale, not just the summary.** Nearly every module
  explains *why it is shaped this way* and what breaks if changed (why raw ASGI middleware,
  why SSE over WebSocket, why `OrjsonResponse` costs the pydantic fast path). Match that, and
  update the reasoning when you change the module. Do not write them as changelogs of a
  session's work — they describe the code as it stands.
- **Tests never depend on the ambient machine.** `tests/conftest.py` stubs the air-infra and
  air-model-gateway probes in *both* directions (`reachable_*` / `unreachable_*`) and points
  every base URL at an `.invalid` host so an escaping call fails loudly. The `app` fixture
  (`build_app` for bespoke settings) patches `model_gateway.generate` with a canned response
  and `model_gateway.stream` with a `scripted_stream`; a test wanting another path reassigns
  them. `tests/integration/` runs the real app against respx doubles that enforce each
  downstream's `extra="forbid"` contract. The
  `client` fixture is in-process (`ASGITransport`, no socket) and deliberately does **not**
  drive the lifespan.
- Nothing renders a raw API key or digest into a log line, repr, or exception message.

## Docs and specs

`docs/00-plan.md` (problem, decisions, phasing) → `docs/01-hld.md` (design; §9 tracks the
cross-repo gaps) → `docs/02-lld.md` (the implementation contract; §15 is the authoritative
build status, and §1's layout marks unbuilt modules with **○**). `docs/openapi.json` is
generated by `make openapi`.

**`docs/specs/` is the normative layer** and this repo is spec-driven: behaviour is specified
before it is built, and every requirement names the test that proves it.

- `docs/specs/constitution.md` — the invariants every spec is checked against. The four
  architectural invariants above are Articles I–IV. Amend it, don't edit it.
- `docs/specs/NNNN-slug/{spec,plan,tasks}.md` — numbered, never renumbered. Requirement ids
  (`REQ-MUT-004`) are permanent addresses: cite them in commits, tests and PRs.
- `docs/specs/README.md` — the workflow, and how the spec layer relates to 00/01/02.

Where a spec and a design doc disagree about **behaviour**, the spec wins; about **rationale**,
the design doc wins. Slash commands `/specify`, `/plan`, `/tasks` and `/trace` drive the loop.

**Changing behaviour means changing its spec first.** A spec edited afterwards to match code
already written is just a changelog, and `make trace` cannot tell the difference — you can.

## Where to start on the next phase

The near-term work, in order:

- **Phase 2b — durable sessions.** Redis behind the existing `SessionStore` protocol in
  `memory/session.py`. The key shape is already tenant-namespaced, so this is a backend swap;
  `build_session_store` currently logs a warning and falls back when asked for `redis`.
  Spec `0002-durable-sessions` is still **Draft**, and its `tasks.md` T0 gates all code on
  resolving open questions Q1/Q2 (which change `REQ-SESS-007`/`008`). Start there, not in
  `memory/`.
- **Phase 3 — read path.** air-classifier's and air-rag-service's clients exist on the shared
  `clients/base.py` (specs 0005, 0010, 0011). Next: air-tools behind the same base, a planner
  (schema-constrained JSON decomposition — HLD §9 item 2), inventory discovery from each
  service's `/v1/capabilities`, and parallel fan-out in `GATHER`. air-rag-service scopes by key, so
  `RagSettings.tenant_keys` picks the air-rag-service key by the principal's tenant (`REQ-RAG-016`).
  Spec 0011's open gap is the grounding check (`REQ-RAG-014`).
- **Phase 4 — write path.** `clients/action.py` behind the propose→confirm→execute gate that
  already exists and is tested.

Cross-repo: air-model-gateway's `/v1/generate` has no `tools` field, tracked in
`docs/01-hld.md` §9; it does not block the phases as ordered.
