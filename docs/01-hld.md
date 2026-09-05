# AIR Orchestrator Service — High-Level Design

**Status:** Draft for review · **Companion docs:** [Plan](00-plan.md) · [LLD](02-lld.md)
**Source:** *AIR-PLATFORM — High-Level Architecture, Rev A*, including its independent-review
items 01–08. This document reconciles that diagram with the AIR repositories as they exist.

---

## 1. Reading the diagram against the repos

The architecture diagram is drawn as a **standalone reference architecture** — every
capability a conversational platform needs, in one frame. The AIR estate has already built
several of those capabilities as independent services. So the diagram's internal boxes are
not all air-orchestrator-service modules; some are clients onto something that exists.

Getting this mapping wrong is the expensive mistake available here, because two of the boxes
— **Model Gateway** and **Intent & Sentiment Analysis** — describe services that are running
today. Reimplementing them inside air-orchestrator-service would fork provider keys, cost accounting and
the escalation ladder across two repos.

| Diagram element | Realised as | Note |
| --- | --- | --- |
| Customer API Gateway | **Edge infrastructure**, not this repo | air-orchestrator-service serves the customer route behind it |
| Business API Gateway | **Edge infrastructure**, not this repo | Corp-VPN-only ingress to the same service, different route and profile (§3) |
| Session & Context Manager | air-orchestrator-service module | Built; state is in-process until the Phase 2b Redis backend lands |
| Intent & Sentiment Analysis | **`air-classifier` :8082** + a local intent step | `/v1/sentiment` and `/v1/feedback` are the four-rung ladder; air-orchestrator-service calls, never classifies |
| Guardrails — input / output | air-orchestrator-service module | **New.** Review item 01 |
| Router / Planner | air-orchestrator-service module | The core of this repo |
| Model Gateway | **`air-llm` :8083** | **Already built**, and split out of air-infra after this design was first drafted. Multi-provider routing, fallback, cost, cache, prompt caching, structured output. This service consumes it over HTTP (`clients/llm.py`); it does not rebuild it. Satisfies review item 02 |
| Semantic Cache | air-orchestrator-service module | **New, not built** (Phase 5). Embeddings from air-llm, vectors in Redis brokered by air-infra. Review item 04 |
| Retrieval Client (RAG) | air-orchestrator-service client → **`air-rag` :8087** | Reranking, grounding and citation shape are air-rag's; air-orchestrator-service asks and cites. **air-rag hosts its own air-llm client for embeddings** — air-orchestrator-service never embeds a query itself |
| Tool Executor | air-orchestrator-service client → **`air-tools` :8084** | **Read-only by construction.** Each domain agent's reason → tool-call → interpret loop (Plan's new "agent placement" decision) runs *inside* air-tools, with its own air-llm client — air-orchestrator-service sends one coarse request per sub-task and never sees the intermediate reasoning |
| Conversation Memory | Redis (turns) + Postgres (profile), brokered by air-infra | Review item 03. **In-process today** — the Redis backend is Phase 2b |
| Response Streamer | air-orchestrator-service module | SSE, §5 |
| External RAG / Knowledge | Behind `air-rag` | Vector DB, embedding service, ingestion — not air-orchestrator-service's concern |
| Tools / Live Data | Behind `air-tools` | |
| Action Services | **`air-action` :8085** | **Mutations only**, idempotency keys and approval gates. Review item 07 |
| MCP Server (optional) | Deferred | §8 |
| Inference Backends (hybrid) | Behind `air-llm` | Ollama local, Anthropic/OpenAI cloud. Self-hosted vLLM/TGI is an air-llm provider, added there |
| Cross-cutting: Security & Secrets | Mostly **`air-infra`** | Credential broker, policy engine, secret backend already exist. This service holds its own API-key store (`security/api_keys.py`) |
| Cross-cutting: Observability | Shared convention | Every service ships structlog + Prometheus; air-orchestrator-service owns the per-turn trace that spans them |
| Cross-cutting: Evaluation & Feedback | air-orchestrator-service module + repo | **New.** Prompt registry, canary prompts, feedback capture. Review item 06 |
| Cross-cutting: Scale & Resilience | Split | Circuit breakers here; async action queue in air-action |

**Not on the diagram:** `air-recommender` :8086. It fits the *Tools / Live Data* role — a
read-only capability the planner may select — and is treated as a fourth read-path client
rather than a new architectural element. Confirm this reading.

**The enterprise reference diagram's per-role LLM Inference layer** (separate Router /
AgentReason / AgentInterpret / AgentGenerate / Generative / Embeddings endpoints) needs no new
service to realise. It's air-llm's `routing.yaml` alias table, given a role-scoped alias per
caller — `agent_reason`/`agent_interpret` for air-tools' agents (Plan's agent-placement
decision), `embeddings` for air-rag, whatever this repo's own synthesis step ends up using.
Each alias is a config edit in air-llm, not a deployment.

## 2. System context

```
   Internet clients                          Corporate systems
   web · mobile · 3rd-party                  internal apps · analysts · batch
          │                                          │
          ▼                                          ▼
  ┌───────────────────┐                     ┌───────────────────────┐
  │ CUSTOMER GATEWAY  │  public edge        │ BUSINESS GATEWAY      │  corp VPN
  │ OIDC/API key      │                     │ mTLS + svc identity   │
  │ rate limit, WAF   │                     │ audit, team quotas    │
  └─────────┬─────────┘                     └───────────┬───────────┘
            │  POST /v1/chat (SSE)                      │  POST /v1/query (SSE)
            └───────────────────┬───────────────────────┘
                                ▼
    ┌──────────────────────────────────────────────────────────┐
    │              air-orchestrator-service :8081              │
    │         stateless · autoscaled · one turn engine         │
    │                                                          │
    │    guardrails-in → context → cache → classify → plan     │
    │     → gather → synthesise → guardrails-out → persist     │
    └───────────────────────────┬──────────────────────────────┘
                                │
  ┌──────────────┬──────────────┴──────────────┬──────────────┬──────────────┐
  ▼              ▼              ▼              ▼              ▼              ▼
  air-llm        air-infra      air-classifier air-rag        air-tools      air-action
  :8083          :8080          :8082          :8087          :8084          :8085
  THE ONLY       stores ·       sentiment      retrieval      READ-ONLY      MUTATIONS
  MODEL PATH     secrets        · intent       · citations    calls          · gates
  │
  ▼
  Ollama · Anthropic · OpenAI · self-hosted
```

One service, two ingress paths, one turn engine. The paths differ in profile, not in
pipeline — §3.

## 3. Two channels, one engine

The diagram's two gateways are not cosmetic: public conversational traffic and internal
business queries have different threat models, different answer shapes, and different audit
obligations. Modelling them as two deployments would duplicate the orchestrator; modelling
them as one undifferentiated endpoint would apply the weaker profile to both.

So air-orchestrator-service carries a **channel** on every turn, derived from the authenticated
principal — never from a request header, which the client controls.

| | `customer` | `business` |
| --- | --- | --- |
| Ingress | Customer API Gateway, public | Business API Gateway, corp VPN only |
| Route | `POST /v1/chat` | `POST /v1/query` |
| Caller identity | End user, via OIDC or API key | Service or analyst, via mTLS identity |
| Answer shape | Prose, cited | **Schema-validated structured output**, prose optional (review item 08) |
| Guardrail profile | Full: injection defence, PII redaction both ways, safety filters | Injection defence and grounding checks; PII redaction configurable — internal analysts legitimately query customer records |
| Mutations | Proposal + explicit human confirmation, always | Same gate; high-impact actions additionally require air-action's approval workflow |
| Audit | Standard structured audit | **Immutable compliance audit log** of every query and answer (review item 05) |
| Quota | Per API key | Per team, with cost attribution |

The pipeline is identical. Channel selects a *profile* — guardrail set, output contract,
audit sink, quota bucket, and the tool allow-list — and nothing else branches.

## 4. The turn pipeline

```
POST /v1/chat  {session_id?, message, options?}
  │
  ├─ 1. authenticate → principal, channel, tenant
  ├─ 2. GUARDRAILS IN ── injection scan · PII redaction · policy
  │        └─ blocked ⇒ refusal answer, turn ends clean (never a 5xx)
  ├─ 3. SESSION & CONTEXT ── load turns from Redis, assemble window, tenant-scope
  ├─ 4. SEMANTIC CACHE ── embed the redacted query, ANN lookup in tenant namespace
  │        └─ hit ⇒ jump to step 9. No model call, no fan-out
  ├─ 5. CLASSIFY ── air-classifier: sentiment + topic (fail-soft to unknown)
  ├─ 6. ROUTER / PLANNER ── direct answer | RAG | tools | action | combination
  ├─ 7. GATHER (parallel, per-turn deadline)
  │        ├─ air-rag      retrieval + citations
  │        ├─ air-tools    read-only calls
  │        ├─ air-recommender
  │        └─ air-action   ⇒ PROPOSE ONLY, never executes in this step
  ├─ 8. SYNTHESISE ── air-llm gateway; grounded in step 7 only
  ├─ 9. GUARDRAILS OUT ── grounding/citation check · PII · safety · schema validation
  ├─ 10. PERSIST ── turn to Redis, cache entry, usage and cost
  └─ 11. STREAM ── SSE throughout; every step above emits as it completes
```

**Step 7's `air-tools` call may itself be a multi-step agent turn**, invisible from here: the
domain agent's own reason → tool-call → interpret loop runs inside air-tools, against its own
air-llm client (Plan's agent-placement decision). air-orchestrator-service's per-call budget covers the
whole thing as one deadline; the intermediate reasoning is air-tools' concern, not this
pipeline's.

Steps 2 and 9 are the diagram's bidirectional guardrail box, and review item 01's point is
that they belong *here* rather than at the gateway: a gateway filter sees a request, not a
prompt, and cannot tell whether a retrieved document has been used as an instruction.

Step 4 before step 5 is deliberate. Review item 04 puts semantic-cache hits at 20–40% of
traffic; a hit that still paid for classification, retrieval and a model call has spent most
of what it was meant to save.

## 5. Streaming

**SSE, not WebSocket** — settling review item 08. The turn is strictly one request in, one
ordered stream out, with no client-to-server messaging mid-turn. SSE survives ordinary HTTP
infrastructure (both gateways, proxies, corporate TLS interception) where WebSocket upgrades
routinely do not, and it degrades to a normal response for a client that does not want it.
Confirmation of a proposed mutation is a *new* turn citing a proposal id, not a message
upstream on an open socket, so nothing needs a bidirectional channel.

**What v1 streams — a deliberate, recorded deviation.** The diagram specifies
*token-by-token* streaming. air-llm cannot do that today: `POST /v1/inference` returns a
complete response, and `ModelProvider` has no `stream()` method. Rather than fork a second
model path — which
would cost the centralised routing, cost accounting and cache that review item 02 exists to
protect — v1 streams the **turn lifecycle**: every stage in §4 emits as it completes, and the
answer arrives as one `answer` event.

This closes without a contract change. When air-llm grows a streaming endpoint, this service
emits `answer.delta` events before the terminal `answer`; the event name is reserved in v1
and clients ignore unknown event types. **The gap is in air-llm, and it is tracked there** —
this is the first of two (§9).

Perceptually this matters less than it sounds: on a turn that retrieves and calls tools, the
first stage event lands within ~250 ms while a token-streamed answer could not begin until
after retrieval anyway.

## 6. Mutations: propose → confirm → execute

The read/write split is the platform's central safety property. It is structural: the tool
client can only reach air-tools, and air-tools has no mutating surface. Reaching air-action
requires the proposal path, and there is no code path from a synthesis step to an execution.

```
turn N     planner decides a mutation is warranted
           └─ air-action /v1/actions/propose → validates, prices, returns proposal_id + risk
           └─ SSE `proposal` event; the answer asks for confirmation. Nothing has changed yet.
           └─ proposal stored on the session with a short TTL

turn N+1   user replies
           ├─ confirms, citing proposal_id ⇒ air-action /v1/actions/execute
           │     with an idempotency key; high-risk actions enter air-action's approval queue
           └─ anything else ⇒ proposal cancelled, silently and by default
```

Three properties do the work:

1. **A confirmation cannot be inferred.** It must cite a `proposal_id` the session is holding.
   Text that merely reads as agreement does not execute anything, which is what makes a
   successful prompt injection in turn N a dead end rather than a write.
2. **air-action re-validates independently.** It never trusts that air-orchestrator-service checked;
   air-orchestrator-service's confirmation is evidence, not authority.
3. **Every execution is idempotent and audited.** The idempotency key is derived from the
   proposal id, so a retried stream cannot double-execute.

Review item 07's async queue and approval gates live inside air-action. air-orchestrator-service's
obligation is to surface queue state as stage events rather than block the turn on it.

## 7. Degradation

Most of the estate is unbuilt, and the diagram's own logic — capabilities behind a planner —
makes absence tractable: a missing service removes a *route*, not the service.

| Down | Behaviour |
| --- | --- |
| air-classifier | Sentiment and topic are `unknown`; tone calibration is skipped. Turn unaffected |
| air-rag | Planner drops the RAG route; the answer says it could not consult the knowledge base rather than answering ungrounded |
| air-tools | Affected capabilities leave the tool list; the planner cannot select what is not advertised |
| air-action | Proposals refused with a clear reason. **Never** a fallback direct write |
| air-recommender | Recommendation capability drops out |
| air-infra | Store credentials unavailable. No effect on the turn path today; from Phase 2b, sessions degrade to single-turn |
| Semantic cache / Redis | Cache disabled, sessions degrade to single-turn; the turn still answers |
| **air-llm** | **No synthesis is possible.** The turn degrades to an honest "cannot answer right now" rather than erroring, and `/v1/ready` reports 503 so the replica is taken out of rotation. Accepted single point of failure; mitigated by provider failover inside air-llm, not by a bypass here |

The rule: a downstream failure narrows what the answer can contain and says so. It never
produces a confident answer built on nothing, and it never fails the turn — with the one
honest exception of the model gateway itself.

## 8. Deferred

- **MCP server.** Optional on the diagram, and review item 07 flags it as an expanded attack
  surface. It is a *second* way to reach tools, and the first one does not exist yet. Revisit
  once air-tools and air-action are real, with scoped credentials, sandboxing and an
  allow-list as entry conditions.
- **Multi-region and data residency** (item 08). A deployment-topology decision that needs a
  real target; the tenant scoping in §3 is the part that must exist now so residency can be
  enforced later without a data-model change.
- **Long-term profile store.** Short-term turns land in Redis in Phase 2; the Postgres profile
  half of the memory box waits until a consumer needs it (Plan §4 Q4).

## 9. Gaps this design opens in air-llm

Both are in air-llm, both are needed for the diagram's full shape, and neither blocks the
phases as ordered:

1. **Streaming.** `ModelProvider.stream()` plus a streaming inference endpoint. Unlocks
   token-by-token (§5). *Not required before Phase 5.*
2. **Tool calling.** The unified inference contract carries `messages` and `json_schema` but
   no `tools`. Native tool-calling is the more direct planner mechanism, and cannot be built
   until this exists. **Plan §4 Q1 resolves this in the meantime**: the planner ships as
   schema-constrained JSON decomposition over the `json_schema` support air-llm already has,
   behind the same interface, so adopting native tool-calling later is an implementation swap
   rather than a contract change. *Wanted before Phase 3; not blocking.*
