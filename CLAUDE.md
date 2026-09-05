# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
make dev                   # .venv (python3.12) + `pip install -e ".[all,dev]"`
make env                   # .env from .env.example (seeds one dev API key per channel)
make run                   # uvicorn with reload on :8081
make check                 # ruff + mypy --strict + pytest  ← run before calling work done
make fmt                   # ruff format + ruff check --fix
make cov                   # pytest with term-missing coverage
make openapi               # regenerate docs/openapi.json  ← after any route/schema change
make up / status / logs / down   # docker; `up` requires air-infra's `air-net` to exist
```

Single test: `.venv/bin/pytest tests/unit/test_turns.py::test_name -q` (`pythonpath=["src"]`
and `asyncio_mode=auto` come from `pyproject.toml`, so no env setup is needed).

The 178 tests are hermetic — no service needs to be running. `make run` needs nothing either;
the service boots and serves `/v1/health` with every downstream down.

## Naming

Repo and distribution are `air-orchestrator-service`; the Python package, env prefix and
metric namespace all match it literally:

| | |
| --- | --- |
| package | `src/air_orchestrator_service/` |
| imports | `from air_orchestrator_service.config import Settings` |
| env prefix | `AIR_ORCHESTRATOR_SERVICE__APP__PORT=8081` |
| metrics | `air_orchestrator_service_turns_total` |
| API keys | `airo_` prefix (`airo_local_customer_key`) |

This is the one service in the estate carrying a `-service` suffix — siblings are
`air_classifier` / `AIR_CLASSIFIER__`, `air_rag` / `AIR_RAG__`. Don't "fix" it to match them.

## Architecture

FastAPI service on **:8081**, the conversational front door for the AIR platform. It
orchestrates and owns no capability: models are `air-llm` (:8083, the *only* model path),
stores/secrets `air-infra` (:8080), plus classifier/tools/action/recommender/rag on
:8082–:8087. All are reached over HTTP with `httpx`, never imported as packages.

Request path: `main.create_app` → `api/middleware.py` (raw ASGI, not `BaseHTTPMiddleware`,
because responses are long-lived streams) → `api/deps.require_principal` → `api/v1/*` →
`engine/turn.TurnEngine.run` → an `AsyncIterator[Event]` that is either SSE-framed
(`api/sse.py`) or folded into one `TurnResult` (`engine.turn.collect`).

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
   unvalidatable settings or an unparseable key file. air-llm being down is a 503 on
   `/v1/ready` (which gates on it alone) and a `degraded` turn that still answers — not a
   crash loop, and not a 5xx from `/v1/chat`.
4. **Every key is tenant-namespaced.** `memory/session.py` keys on `(tenant, session_id)` even
   in the in-memory store, because that key shape is what the Phase 2b Redis backend inherits.
   Absent-vs-not-yours are deliberately indistinguishable to the caller (both 404).

### The turn engine (`engine/turn.py`, ~780 lines — the centre of the service)

Stages run in `constants.Stage` order: `guardrails_in → context → cache → classify → plan →
gather → synthesise → guardrails_out → persist`. Each emits a `stage` event with a
`StageStatus`. **A stage this build cannot really run reports `skipped` with a reason, never
`ok`** — `_STUBBED` is the single list, and deleting an entry there is what "turning a stage
on" means. `blocked` (a guardrail refusing) is deliberately distinct from `degraded` (a
downstream coping) so a refusal spike does not look like an outage.

Real today: guardrails both ways, prompt registry, synthesis via `LlmClient.chat`, per-turn
and per-session cost ceilings, a turn deadline decremented before the synthesis call. Stubbed:
`CACHE` (Phase 5), `CLASSIFY`/`GATHER` (Phase 3). Routing is still the literal `/propose`
trigger — a stand-in must not appear to understand intent it does not.

### Contracts that are public surface

- **`constants.py`** — `Stage`, `Route`, `Channel`, `EventType`, `TurnStatus`, scopes. These
  appear in API responses *and* Prometheus label values, so they are closed sets and renaming
  a member is a breaking change for callers and dashboards alike.
- **`schemas/events.py`** — the SSE models *are* the contract. `turn.start`/`turn.end` bracket
  every turn including refused and errored ones; clients must ignore unknown event names (that
  is what lets `answer.delta` — defined, never emitted — arrive later without a version bump).
  v1 streams the turn lifecycle, not tokens, because air-llm cannot stream yet.
- **`api/errors.py`** — every failure leaves as RFC 9457 `application/problem+json`; nothing
  internal escapes (the catch-all logs the traceback and returns a fixed 500). A failure
  *after* headers are sent is an `error` **event**, not a status — that module handles only
  pre-first-byte failures.

### Configuration

`config.py` is nested pydantic-settings with prefix `AIR_ORCHESTRATOR_SERVICE__` and `__` as
delimiter. `create_app(settings)` takes settings as an argument and never reads the
environment itself — tests assemble a full app with bespoke settings rather than mutating the
environment or clearing `get_settings`' cache. Budgets and guardrail profiles live in settings
so tuning cost/latency never requires a deploy. Every optional downstream defaults to
`enabled=false`: a fresh checkout must not report itself degraded against services that do not
exist.

## Conventions

- **mypy `strict` on `src`, ruff at line-length 100** with `S` (bandit), `T20` (no `print`),
  `ASYNC`, `B`, `SIM`, `UP` enabled. Structured logging via `structlog` (`get_logger`), never
  `print`. The long package name puts multi-name imports over 100 chars — let `make fmt`
  wrap them.
- **Module docstrings carry the rationale, not just the summary.** Nearly every module
  explains *why it is shaped this way* and what breaks if changed (why raw ASGI middleware,
  why SSE over WebSocket, why `OrjsonResponse` costs the pydantic fast path). Match that, and
  update the reasoning when you change the module. Do not write them as changelogs of a
  session's work — they describe the code as it stands.
- **Tests never depend on the ambient machine.** `tests/conftest.py` stubs the air-infra and
  air-llm probes in *both* directions (`reachable_*` / `unreachable_*`) and points their base
  URLs at `.invalid` hosts so an escaping probe fails loudly. The `app` fixture patches
  `LlmClient.chat` with a canned response; a test wanting the failure path reassigns it.
- Nothing renders a raw API key or digest into a log line, repr, or exception message.

## Where to start on the next phase

`docs/02-lld.md` §15 is the authoritative build status, and §1's layout marks unbuilt modules
with **○**. The near-term work, in order:

- **Phase 2b — durable sessions.** Redis behind the existing `SessionStore` protocol in
  `memory/session.py`. The key shape is already tenant-namespaced, so this is a backend swap;
  `build_session_store` currently logs a warning and falls back when asked for `redis`.
- **Phase 3 — read path.** Clients for air-classifier/air-rag/air-tools/air-recommender behind
  a shared `clients/base.py`, capability discovery, and turning `CLASSIFY`/`GATHER` real. The
  resilience primitives are meant to be lifted from air-classifier's `resilience/`, not
  rewritten.
- **Phase 4 — write path.** `clients/action.py` behind the propose→confirm→execute gate that
  already exists and is tested.

Cross-repo: air-llm has no `tools` field on its inference contract and no streaming endpoint.
Both are tracked in `docs/01-hld.md` §9; neither blocks the phases as ordered.
