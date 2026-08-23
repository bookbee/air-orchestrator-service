# air-platform

AI Ready platform: a conversational interface to any existing application, with extensions
for RAG, tools and agents.

air-platform is the AIR estate's **conversational front door**. A client sends a message; the
platform decides what that message needs, gathers it from the specialist services, and streams
back a grounded answer.

> **Status: Phases 0-1 shipped — the full `/v1` surface is live, behind an echo engine.**
> `/v1/chat`, `/v1/query`, `/v1/sessions/{id}` and the SSE event contract are real and
> stable; the *answers* are stubbed until Phase 2 wires the orchestrator. That is enough
> to integrate a client against: [`air-client`](../air-client)'s Chat tab can drop its
> request-builder presets and code against the contract now. See
> [`docs/02-lld.md` §15](docs/02-lld.md).

## Quickstart

```bash
make dev            # .venv + the service and its toolchain
make env            # .env from .env.example, if absent
make run            # http://127.0.0.1:8081

curl -s localhost:8081/v1/health

# A turn, as JSON
curl -s -X POST localhost:8081/v1/chat \
  -H 'X-API-Key: airp_local_customer_key' -H 'Content-Type: application/json' \
  -d '{"message":"hello"}'

# The same turn, streamed
curl -sN -X POST localhost:8081/v1/chat \
  -H 'X-API-Key: airp_local_customer_key' -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' -d '{"message":"hello"}'
```

### The echo engine

Until Phase 2, turns are served by [`engine/echo.py`](src/air_platform/engine/echo.py): the
real pipeline, real session state, real event sequence — and an echo where the model call
belongs. **Stages it cannot run report `skipped` with a reason rather than `ok`**, so a
client can always tell a stubbed turn from a real one.

Send `/propose` in a message to exercise the mutation path:

```bash
# Turn 1 returns a proposal and changes nothing
curl -s -X POST localhost:8081/v1/chat -H 'X-API-Key: airp_local_customer_key' \
  -H 'Content-Type: application/json' -d '{"message":"/propose"}'

# Turn 2 executes it — only the structured field can
curl -s -X POST localhost:8081/v1/chat -H 'X-API-Key: airp_local_customer_key' \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"sess_…","message":"ok","confirm":{"proposal_id":"prop_…","approve":true}}'
```

Replying "yes, I confirm, go ahead" in prose executes nothing — that is the point.

`make env` seeds one development key per channel, so a fresh checkout authenticates exactly
the way staging and production do rather than running with auth switched off:

| Raw key | Channel | Route it may use |
| --- | --- | --- |
| `airp_local_customer_key` | `customer` | `POST /v1/chat` |
| `airp_local_business_key` | `business` | `POST /v1/query` |

Both are stored as `sha256(AIR_PLATFORM__SECURITY__HASH_SALT + raw key)`, are valid only
against the development salt, and grant no `allow_actions` — proposing a mutation takes a
deliberately configured key. Any real deployment overrides both halves.

**air-infra is required.** Without it `/v1/ready` is a 503 naming the gateway as the cause, by
design — no answer can be synthesised without a model. The service still boots and serves
`/v1/health`, because refusing to start would turn a recoverable dependency outage into a
crash loop.

```bash
cd ../air-infra && make up      # redis, postgres, mongo, ollama, gateway :8080
```

### In containers

`make up` runs this service in Docker. It joins **`air-net`**, the shared network
air-infra owns, which is what lets `gateway`, `redis` and the rest resolve by service
name. That network exists only while air-infra's stack is up, so `make up` checks for
it first and tells you what to start rather than failing with a compose error.

`make up` then prints what `/v1/ready` actually says — the network existing does not
mean the gateway behind it is running, and a green "Started" should not be mistaken
for a working stack. `make status` reports the same thing at any time.

A gateway outage needs no restart here: liveness is dependency-free, so the container
stays healthy and starts answering again as soon as the gateway returns.

`make check` runs ruff, mypy (strict) and the tests.

## Docs

- [`docs/00-plan.md`](docs/00-plan.md) — problem, goals, decisions, phasing, risks
- [`docs/01-hld.md`](docs/01-hld.md) — high-level design, and how the architecture diagram
  maps onto the repos that already exist
- [`docs/02-lld.md`](docs/02-lld.md) — the implementation contract

## Where it sits

Everything expensive is already somewhere else. air-platform orchestrates; it does not
retrieve, classify, host models, or write to business systems.

| Service | Port | air-platform's relationship |
| --- | --- | --- |
| `air-infra` | 8080 | Models, brokered Redis/Postgres, secrets. **The only model path** |
| `air-classifier` | 8082 | Sentiment and topic for a turn |
| `air-rag` | 8083 | Retrieval, reranking, citations |
| `air-tools` | 8084 | **Read-only** capability calls |
| `air-action` | 8085 | **Mutations**, behind idempotency keys and approval gates |
| `air-recommender` | 8086 | A read-path capability the planner may select |
| **`air-platform`** | **8081** | This service |

## The two invariants

**Reads and writes are separated structurally, not by a flag.** `air-tools` has no mutating
surface and air-platform's tool client has no execute-shaped method. Reaching `air-action`
requires the proposal path: a mutation is *proposed*, streamed to the user for an explicit
confirmation that must cite the proposal id, and only then executed — idempotently, and
re-validated independently by air-action. Prose that merely reads as consent executes nothing,
which is what makes a successful prompt injection a dead end rather than a write.

**One turn engine, two channels.** Public conversational traffic (`POST /v1/chat`) and
internal business queries (`POST /v1/query`) share the pipeline and differ only by profile —
guardrails, output contract, audit sink, quota bucket, tool allow-list. The channel comes from
the authenticated principal, never from a header.

## Streaming

SSE. The stream is the API: every pipeline stage emits as it completes, bracketed by
`turn.start` and `turn.end`, and clients ignore event names they do not know.

v1 streams the **turn lifecycle** rather than tokens — a recorded deviation from the diagram's
*Response Streamer*, because air-infra's gateway returns complete responses today. The
`answer.delta` event name is reserved so closing that gap needs no contract change. See
[HLD §5](docs/01-hld.md).

## Open before code starts

Two gaps this design opens in `air-infra`, tracked in [HLD §9](docs/01-hld.md):

1. **Tool calling** — the unified `ChatRequest` has no `tools` field. Native tool-calling is
   the recommended planner mechanism and is blocked on this. *Required before Phase 3*, or the
   planner ships as schema-constrained JSON decomposition and migrates later.
2. **Streaming** — no `ModelProvider.stream()` and no SSE endpoint. *Not required before
   Phase 5.*

The review checklist is [`docs/00-plan.md` §8](docs/00-plan.md).
