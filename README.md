# air-orchestrator-service

The AIR platform's **conversational front door**. A client sends a message; this service
decides what that message needs, gathers it from the specialist services, and streams back a
grounded answer.

It orchestrates and owns no capability of its own — retrieval is `air-rag`'s, classification
`air-classifier`'s, read-only calls `air-tools`', mutations `air-action`'s, models `air-llm`'s,
stores `air-infra`'s.

> **Status.** Phases 0, 1 and 2a are shipped: the full `/v1` surface, the SSE event contract,
> guardrails in both directions, the versioned prompt registry, real synthesis through
> air-llm, and enforced turn/session budgets. Classification, downstream fan-out and the
> semantic cache are **not** built — those stages report `skipped` with a reason rather than
> `ok`, so a client can always tell a stubbed turn from a real one. See
> [`docs/02-lld.md` §15](docs/02-lld.md) for the phase-by-phase state.

## Quickstart

```bash
make dev            # .venv + the service and its toolchain
make env            # .env from .env.example, if absent
make run            # http://127.0.0.1:8081

curl -s localhost:8081/v1/health
```

A turn, as JSON and then streamed:

```bash
curl -s -X POST localhost:8081/v1/chat \
  -H 'X-API-Key: airo_local_customer_key' -H 'Content-Type: application/json' \
  -d '{"message":"hello"}'

curl -sN -X POST localhost:8081/v1/chat \
  -H 'X-API-Key: airo_local_customer_key' -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' -d '{"message":"hello"}'
```

`make check` runs ruff, mypy (strict) and the tests. **The tests need nothing running** — every
air-infra and air-llm probe is stubbed in both directions, deliberately, so a result never
depends on what happens to be up on your machine.

### Dependencies

**air-llm is the one dependency a real answer requires**, and `/v1/ready` gates on it alone.
air-infra is reported on `/v1/ready` for operator visibility but does not affect readiness —
it brokers Redis/Postgres/Mongo credentials, which nothing on the turn path uses yet (the
session store is in-process until Phase 2b).

```bash
cd ../air-llm   && make up      # the model gateway :8083 — needed for a real answer
cd ../air-infra && make up      # redis, postgres, mongo :8080 — needed from Phase 2b
```

The service boots and serves `/v1/health` with both of them down. It reports itself unready
and degrades individual turns instead of refusing to start, because refusing to start would
turn a recoverable dependency outage into a crash loop.

### Development API keys

`make env` seeds one key per channel, so a fresh checkout authenticates exactly the way
staging and production do rather than running with auth switched off:

| Raw key | Channel | Route it may use |
| --- | --- | --- |
| `airo_local_customer_key` | `customer` | `POST /v1/chat` |
| `airo_local_business_key` | `business` | `POST /v1/query` |

Both are stored as `sha256(AIR_ORCHESTRATOR_SERVICE__SECURITY__HASH_SALT + raw key)`, are
valid only against the development salt, and grant no `allow_actions` — proposing a mutation
takes a deliberately configured key. Any real deployment overrides both halves.

### Exercising the mutation path

`/propose` in a message is a literal trigger that makes the engine propose a mutation, so the
propose → confirm → execute path can be walked end to end before `air-action` exists:

```bash
# Turn 1 returns a proposal and changes nothing
curl -s -X POST localhost:8081/v1/chat -H 'X-API-Key: airo_local_customer_key' \
  -H 'Content-Type: application/json' -d '{"message":"/propose"}'

# Turn 2 executes it — only the structured field can
curl -s -X POST localhost:8081/v1/chat -H 'X-API-Key: airo_local_customer_key' \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"sess_…","message":"ok","confirm":{"proposal_id":"prop_…","approve":true}}'
```

Replying "yes, I confirm, go ahead" in prose executes nothing — that is the point.

### In containers

`make up` runs this service in Docker on **`air-net`**, the shared network air-infra owns,
which is what lets `gateway`, `redis` and the rest resolve by service name. That network
exists only while air-infra's stack is up, so `make up` checks for it first and tells you what
to start rather than failing with a compose error. air-llm is reached over the host, since it
is never on `air-net`.

`make up` then prints what `/v1/ready` actually says, because the network existing does not
mean the services behind it are running and a green "Started" should not be mistaken for a
working stack. `make status` reports the same thing at any time.

The container healthcheck is liveness only. A dependency outage needs no restart: the
container stays healthy and turns start succeeding again as soon as air-llm returns.

## Where it sits

| Service | Port | This service's relationship |
| --- | --- | --- |
| `air-infra` | 8080 | Brokers Redis/Postgres/Mongo credentials and secrets. **Not** the model path |
| **`air-orchestrator-service`** | **8081** | This service |
| `air-classifier` | 8082 | Sentiment and topic for a turn (client not built — Phase 3) |
| `air-llm` | 8083 | Model gateway, reached directly, never via air-infra. **The only model path** (G7) |
| `air-tools` | 8084 | **Read-only** capability calls (Phase 3) |
| `air-action` | 8085 | **Mutations**, behind idempotency keys and approval gates (Phase 4) |
| `air-recommender` | 8086 | A read-path capability the planner may select (Phase 3) |
| `air-rag` | 8087 | Retrieval, reranking, citations (Phase 3) |

## The two invariants

**Reads and writes are separated structurally, not by a flag.** `air-tools` has no mutating
surface and this service's tool client will have no execute-shaped method. Reaching
`air-action` requires the proposal path: a mutation is *proposed*, streamed to the user for an
explicit confirmation that must cite the proposal id, and only then executed — idempotently,
and re-validated independently by air-action. Prose that merely reads as consent executes
nothing, which is what makes a successful prompt injection a dead end rather than a write.

**One turn engine, two channels.** Public conversational traffic (`POST /v1/chat`) and internal
business queries (`POST /v1/query`) share the pipeline and differ only by profile — guardrails,
output contract, audit sink, quota bucket, tool allow-list. The channel comes from the
authenticated principal, never from a header: a client that could name its own channel could
select the weaker guardrail profile, which is the whole thing the split exists to prevent.

## Streaming

SSE, content-negotiated: send `Accept: text/event-stream` to stream, anything else to get the
terminal result as one JSON body. Same engine either way. Every pipeline stage emits as it
completes, bracketed by `turn.start` and `turn.end`, and clients must ignore event names they
do not know.

v1 streams the **turn lifecycle** rather than tokens, because air-llm returns complete
responses today. The `answer.delta` event name is reserved and documented so closing that gap
needs no contract change — see [HLD §5](docs/01-hld.md).

## Docs

- [`docs/00-plan.md`](docs/00-plan.md) — problem, goals, decisions, phasing, risks
- [`docs/01-hld.md`](docs/01-hld.md) — high-level design and the diagram-to-repo mapping
- [`docs/02-lld.md`](docs/02-lld.md) — the implementation contract, and §15's build status
- [`docs/openapi.json`](docs/openapi.json) — generated; refresh with `make openapi`
- [`CLAUDE.md`](CLAUDE.md) — orientation for AI coding agents working in this repo

## Known gaps in air-llm

Two things this design needs that `air-llm` does not offer yet. Neither blocks the phases as
ordered, and both are tracked in [HLD §9](docs/01-hld.md):

1. **Tool calling** — the unified inference contract has no `tools` field. Until it does, the
   planner ships as schema-constrained JSON decomposition (which air-llm already supports via
   `json_schema`) behind the same interface, so the migration is an implementation swap rather
   than a contract change. *Wanted before Phase 3.*
2. **Streaming** — no `ModelProvider.stream()` and no SSE endpoint, so `answer.delta` cannot be
   emitted. *Not required before Phase 5.*
