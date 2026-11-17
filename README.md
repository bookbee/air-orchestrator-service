# air-orchestrator-service

The AIR platform's **conversational front door**. A client sends a message; this service
decides what that message needs, gathers it from the specialist services, and streams back a
grounded answer.

It orchestrates and owns no capability of its own: document search is air-rag-service's,
classification air-classifier-service's, lookups and changes to business systems
air-tools-gateway's, models air-model-gateway's.

> **Part of the AIR platform.** New here? Start with the
> [platform overview](../air-web-console/docs/platform/01-overview.md) (how the services fit
> together, with diagrams), the [concepts page](../air-web-console/docs/platform/02-concepts.md)
> (planner, guardrails, RAG and the other AI terms used below) and the
> [local setup guide](../air-web-console/docs/platform/03-local-setup.md) in air-web-console.

| If you are… | Start with |
| --- | --- |
| New to the service | [What a turn does](#what-a-turn-does), then [Quickstart](#quickstart) |
| Calling the API | [Quickstart](#quickstart), [Streaming](#streaming), [`docs/openapi.json`](docs/openapi.json) |
| Running it with the other services | [Dependencies](#dependencies) |
| Deploying it | [Deployment notes](#deployment-notes) |
| Changing the code | [`CLAUDE.md`](CLAUDE.md), then [`docs/specs/README.md`](docs/specs/README.md) |
| Picking up open work | [`docs/03-uat-open-items.md`](docs/03-uat-open-items.md) |

> **Status (2026-11-16).** Built and verified end to end against the other services: the full
> `/v1` surface, the streamed event contract, guardrails in both directions, versioned prompts,
> answers through air-model-gateway, enforced turn and session budgets, classification
> (spec 0005), document search with citations (spec 0011), order lookups and confirmed
> changes through air-tools-gateway (spec 0012), the model planner (spec 0013) and customer
> tokens (spec 0014). **Every optional service is off by default** and switched on by
> configuration. **Not built:** durable sessions (so the service runs as one process and one
> replica), the semantic cache, the grounding check, and any data source for the business
> channel. A stage that didn't really run reports `skipped` with a reason, never `ok`.
> [`docs/02-lld.md` §15](docs/02-lld.md) is the authoritative build status.

## What a turn does

Every message runs through the same stages, in this order. The response lists each one with
its status, so you can always see what really happened.

| Stage | What it does | Needs |
| --- | --- | --- |
| `guardrails_in` | Refuses prompt injection; masks card numbers, emails and phones; detects a request for a human | nothing |
| `context` | Loads the conversation so far | the in-memory session store |
| `cache` | Not built: always `skipped` | |
| `classify` | Sentiment, intent and urgency (customer channel) | air-classifier-service, if enabled |
| `plan` | Decides which lookups, or which change, the message needs | air-model-gateway, if the planner is enabled |
| `gather` | Runs lookups, searches documents, proposes or executes a change | air-tools-gateway and air-rag-service, if enabled |
| `synthesise` | The model writes the answer from what was gathered | air-model-gateway (**required**) |
| `guardrails_out` | Masks personal data in the answer; drops source markers that name no source | nothing |
| `persist` | Saves the turn | the in-memory session store |

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

`make check` runs ruff, mypy (strict), the tests and the spec traceability check. **The tests
need nothing running**: every downstream is stubbed in both directions, deliberately, so a
result never depends on what happens to be up on your machine.

### Dependencies

| Service | Needed? | What you lose without it |
| --- | --- | --- |
| air-model-gateway (:8083) | **Required for an answer.** `/v1/ready` gates on it alone: on whether it serves *this service* (token accepted, model class granted and backed by a provider), not merely whether it is up | Turns report `degraded` and answer with a fallback |
| air-classifier-service (:8082) | Optional, off by default | `classify` is `skipped` |
| air-rag-service (:8087) | Optional, off by default | No document search, no citations |
| air-tools-gateway (:8084) | Optional, off by default | No order lookups, no changes |
| air-infrastructure-service (:8080) | Only for `make up`: it owns the Docker network `air-net`. Its health is shown on `/v1/ready` (as `air-infra`) but doesn't affect readiness | `make up` stops and says so; `make run` on the host needs nothing from it |

```bash
cd ../air-infrastructure-service && make env && make up   # air-net; redis, postgres, mongo
cd ../air-model-gateway && make up                        # the model gateway :8083
```

The service boots and serves `/v1/health` with all of them down. It reports itself unready
and degrades individual turns instead of refusing to start, because refusing to start would
turn a recoverable dependency outage into a crash loop.

### Switching the optional services on

A fresh `.env` (from `make env`) has the classifier, document search, the tools gateway and
the planner **off**, and answers with the gateway's `local` model class. That is deliberate:
a single checkout must not report itself degraded against services that aren't running. To
run the integrated platform locally, the quickest way is from air-web-console:

```bash
cd ../air-web-console && make platform-env          # appends a marked block to this repo's .env
cd ../air-orchestrator-service && make up           # .env is read once, at start-up
```

The block it appends is
[`air-web-console/env/local/air-orchestrator-service.env`](../air-web-console/env/local/air-orchestrator-service.env).
In short, it sets:

| Setting (`AIR_ORCHESTRATOR_SERVICE__…`) | Value | Why |
| --- | --- | --- |
| `DOWNSTREAM__CLASSIFIER__ENABLED`, `…__API_KEY` | `true`, `airc_local_dev_key` | Turns `classify` on |
| `DOWNSTREAM__RAG__ENABLED`, `…__API_KEY` | `true`, `airr_local_dev_key` | Document search and citations |
| `DOWNSTREAM__TOOLS_GATEWAY__ENABLED`, `…__TENANT_KEYS`, `…__ALLOW__CUSTOMER` | `true`, the gateway's development read and execute keys, `["orders.*"]` | Order lookups and changes |
| `PLANNER__ENABLED` | `true` | Without it no lookup is ever planned |
| `MODEL_GATEWAY__MODEL_CLASS`, `PLANNER__MODEL_CLASS` | `cloud` | Needs a cloud provider key in the gateway's `.env` |
| `SECURITY__API_KEYS_INLINE` | The customer key with `allow_actions: true` | Without it a change is never offered |

`curl -s localhost:8081/v1/ready` lists each dependency with `configured: true` once they are on.

### Development API keys

`make env` seeds one key per channel, so a fresh checkout authenticates exactly the way
staging and production do rather than running with auth switched off:

| Raw key | Channel | Route it may use |
| --- | --- | --- |
| `airo_local_customer_key` | `customer` | `POST /v1/chat` |
| `airo_local_business_key` | `business` | `POST /v1/query` |

Both are stored as `sha256(AIR_ORCHESTRATOR_SERVICE__SECURITY__HASH_SALT + raw key)`, are
valid only against the development salt, and grant no `allow_actions` — proposing a mutation
takes a deliberately configured key. Both are on tenant `brightcart`, the tenant
air-tools-gateway's development keys and air-web-console's development customer tokens are
for. Any real deployment overrides both halves.

### Chatting as a customer

A customer key names a storefront, not a shopper. To chat *as* a signed-in customer, send a
short-lived customer token beside it in the `air-token` header (spec 0014): an ES256 JWT,
`typ` `air-customer+jwt`, whose `sub` becomes the turn's customer. `make env` configures
air-web-console's development public key (`SECURITY__CUSTOMER_TOKENS`), so the console's
Customer picker (`cust_1001`, `cust_1002`) works locally with no edits.

- **No header:** an anonymous customer. The turn answers; per-customer lookups are not offered.
- **A header that fails any check** (expired, wrong tenant, tampered, unknown key): `401
  invalid-customer-token`, one body for every reason. The reason is logged and counted
  (`air_orchestrator_service_customer_tokens_total{outcome}`), never returned.
- **A session belongs to the customer who started it.** Another customer, or no token, on
  the same session gets the same `404` as a session that does not exist, so switching
  customers mid-conversation starts a new session.
- `/v1/query` ignores the header.

### Exercising the mutation path

`/propose` in a message is a literal trigger that makes the engine propose a no-op mutation, so
the propose → confirm → execute gate can be walked end to end. It needs a key whose record sets
`"allow_actions": true`: the development keys don't, and get an ordinary answer instead
(spec 0001, `REQ-MUT-012`).

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

`make up` runs this service in Docker on **`air-net`**, the shared network
air-infrastructure-service owns, which is what lets `air-classifier-service`,
`air-tools-gateway` and the rest resolve by service name. That network exists only while that
stack is up, so `make up` checks for it first and tells you what to start rather than failing
with a compose error. air-model-gateway and air-rag-service are reached over the host
(`host.docker.internal`).

`make up` then prints what `/v1/ready` actually says, because the network existing does not
mean the services behind it are running and a green "Started" should not be mistaken for a
working stack. `make status` reports the same thing at any time.

The container healthcheck is liveness only. A dependency outage needs no restart: the
container stays healthy and turns start succeeding again as soon as air-model-gateway returns.

## Where it sits

| Service | Port | This service's relationship |
| --- | --- | --- |
| air-infrastructure-service | 8080 | Local development stack (Redis, Postgres, MongoDB) and owner of `air-net`. Probed for health only; reported as `air-infra`. **Not** the model path |
| **air-orchestrator-service** | **8081** | This service |
| air-classifier-service | 8082 | Sentiment, intent and urgency for a customer turn (`classify`; optional, fail-soft; spec 0005) |
| air-model-gateway | 8083 | The model gateway, reached directly. **The only model path** (constitution, Article VII) |
| air-tools-gateway | 8084 | Node REST gateway to business systems: lookups, and changes behind propose → confirm → execute. No model inside it (spec 0012) |
| air-recommender | 8086 | Not built. A read-path capability reserved for later |
| air-rag-service | 8087 | Document search with citations (spec 0011) |

The names `air-infra`, `air-classifier` and `air-rag` appear in this service's API responses
and metric labels. They are wire labels and stay as they are, whatever the repositories are
called.

## The two invariants

**Reads and writes are separated by protocol and credential, not by a flag.** One gateway,
`air-tools-gateway`, serves both, on separate endpoints and separately scoped keys. This
service's lookup client has no execute-shaped method and never holds the execute key.
Reaching execute requires the proposal path: a mutation is *proposed*, streamed to the user
for an explicit confirmation that must cite the proposal id, and only then executed, once,
with an idempotency key, and re-validated independently by the gateway (spec 0012). Prose that merely reads as consent executes
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

A streamed turn also carries the answer as **`answer.delta`** events while the model writes it
(spec 0004). Every delta has passed the output PII guardrail through a hold-back buffer, so text
the final scan would redact never streams. Deltas are **provisional**: the `answer` event that
follows is authoritative. It equals the concatenated deltas on a normal turn, and carries the
refusal or fallback text when the provider refused, the stream broke or the deadline passed.
A client that renders deltas must replace them with `answer`. A JSON caller's turn makes a
non-streaming call and keeps the gateway's full pre-first-token failover.

## Deployment notes

```bash
export IMAGE_REPOSITORY=<region>-docker.pkg.dev/<project>/air/air-orchestrator-service
make image-push                                   # linux/amd64, tagged with this commit
make deploy DEPLOY_ENV=uat KUBE_CONTEXT=<context> GCP_PROJECT=<project>
make deploy-smoke DEPLOY_ENV=uat KUBE_CONTEXT=<context>
```

This repository owns its `Dockerfile` and image. The Helm chart, the values for UAT
and production, the Terraform and the deploy scripts are in
[air-infrastructure-service](../air-infrastructure-service/gke/README.md) (`gke/`),
where DevOps keeps them for every core service; the targets above call into that
checkout (`INFRA_DIR`). Its [runbook](../air-infrastructure-service/docs/03-gke-runbook.md)
is the procedure, and `make deploy-render SAMPLE=1` prints what would be applied.
**If this service's start command, port or user changes, update its values files
there** (`gke/values/*/air-orchestrator-service.yaml`).

There is no CI workflow and no deploy pipeline in this repository yet. The points that
matter most for a deployment:

- **Run exactly one replica.** Sessions and pending proposals live in process memory, so a
  second replica (or a second worker) answers half of all follow-ups with a 404, and a restart
  drops every conversation in progress. The process starts one worker whatever
  `APP__WORKERS` says (spec 0015). Durable sessions are spec 0002, not built.
- **The container listens on 8081** and runs as uid 10001. Liveness is `/v1/health`;
  readiness is `/v1/ready`. The image has no `HEALTHCHECK` on purpose: declare probes in the
  manifest.
- **Rate limits are configured but not enforced** (air-web-console bug OR-017). Limit at the
  ingress until they are.
- **Every key in `.env.example` is a development value.** A deployment needs its own
  `HASH_SALT`, API key records, downstream keys and customer-token public keys.
  `APP__ENV=production` refuses the unauthenticated escape hatch.

## Docs

- [`docs/00-plan.md`](docs/00-plan.md): problem, goals, decisions, phasing, risks
- [`docs/01-hld.md`](docs/01-hld.md): high-level design and the diagram-to-repo mapping
- [`docs/02-lld.md`](docs/02-lld.md): the implementation contract, and §15's build status
- [`docs/03-uat-open-items.md`](docs/03-uat-open-items.md): what stands between this build and UAT
- [`docs/specs/`](docs/specs/README.md): the normative layer. One numbered spec per behaviour;
  every requirement names the test that proves it (`make trace`)
- [`docs/openapi.json`](docs/openapi.json): generated; refresh with `make openapi`
- [`CLAUDE.md`](CLAUDE.md): the compact engineering guide (invariants, wiring, conventions),
  also used by AI coding assistants
- Bugs found in this service are tracked in
  [`air-web-console/bugs/orchestrator/`](../air-web-console/bugs/orchestrator/README.md)

## Known gap in air-model-gateway

One thing this design needs that air-model-gateway does not offer yet. It does not block the
phases as ordered, and it is tracked in [HLD §9](docs/01-hld.md):

1. **Tool calling** — `/v1/generate` has no `tools` field. Until it does, the planner ships as
   schema-constrained JSON decomposition (which the gateway supports via `json_schema`) behind
   the same interface, so the migration is an implementation swap rather than a contract
   change. *Wanted before Phase 3's planner.*

Streaming, the other gap tracked against the retired air-llm, is closed: the gateway streams, and
this service relays it (spec 0004).
