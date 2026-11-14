# AIR Orchestrator Service — Plan

**Status:** Baseline · **Owner:** Vikas Roy · **Last reviewed:** 2026-09-06
**Companion docs:** [HLD](01-hld.md) · [LLD](02-lld.md)
**Source:** *AIR-PLATFORM — High-Level Architecture, Rev A*, and its independent-review
items 01–08. The HLD reconciles that diagram against the AIR repositories.

---

## 1. Problem statement

`air-orchestrator-service` is the AIR platform's **conversational front door**. A client sends a message;
air-orchestrator-service decides what that message needs, gathers it from the specialist services, and
streams back a grounded reply.

Everything expensive or specialised already lives somewhere else — retrieval in `air-rag-service`,
classification in `air-classifier`, recommendations in `air-recommender`, lookups and
changes to business systems behind `air-tools-gateway`, models behind `air-model-gateway`,
stores behind `air-infra`. What does *not*
exist is the thing that turns "where is my order and can you cancel it" into a sequence of
calls across those services and one coherent answer.

Three constraints define the design:

**The read/write split is enforced by protocol and credential, not by a repo boundary.**
`air-tools-gateway` serves both lookups and changes, and keeps them apart the way a bank
keeps a balance enquiry apart from a transfer: different endpoints, different keys, and a
change that is only ever *proposed* until a confirmation arrives. A `write` capability has a
propose endpoint that changes nothing and an execute endpoint that only a separately scoped
execute key can reach; this service holds that key on the confirmation path alone. A model
that has been talked into calling `orders.cancel` can at most produce a proposal, which a
customer must confirm by a structured field the model cannot write, and which the gateway
re-validates before acting. The plan first drew this boundary as two services, air-tools and
air-action; §4 "Decided: one tools gateway" records why it is now one, and what carries the
boundary instead.

**The reply is streamed.** A turn that fans out to three services and a model takes seconds.
A client that must wait for all of it before rendering anything feels broken, so the API
streams the orchestration as it happens — routing decided, retrieval done, action awaiting
confirmation, answer — rather than a single response at the end.

**There are two kinds of caller, not one.** Public conversational traffic arrives through the
Customer API Gateway; internal analysts, batch jobs and corporate applications arrive through
the Business API Gateway on the corporate network. They want different answer shapes (prose
versus schema-validated structured output), carry different threat models, and sit under
different audit obligations. One turn engine serves both, with a `channel` on every turn
selecting the profile — see [HLD §3](01-hld.md).

## 2. Goals

| # | Goal | Acceptance signal |
| --- | --- | --- |
| G1 | One conversational API for the whole platform | A client integrates against air-orchestrator-service alone and reaches every AIR capability |
| G2 | Streaming turn lifecycle | Client renders the first event well before the final answer; every stage is observable as it completes |
| G3 | Strict read/write separation | Every mutation is proposed, confirmed and executed through air-tools-gateway's write path, on a separately scoped execute key; air-orchestrator-service holds no mutating capability of its own, and this is enforced by the client layer, not by convention (spec 0012) |
| G4 | Confirmed, auditable mutations | No mutating call executes without an explicit confirmation step recorded against the session |
| G5 | Degrades service by service | Any downstream being absent removes a capability from the turn; it never fails the turn |
| G6 | Durable multi-turn sessions | Conversation state survives restart and is shared across replicas, via air-infra's brokered Redis |
| G7 | All model traffic through air-model-gateway (constitution Article VII) | No provider SDK and no provider key in this repo; cost and cache stay centralised |
| G8 | Guardrails on both directions, inside the platform | Injection defence, PII redaction and grounding checks run on input *and* output, where the prompt is visible — not at the gateway, which only sees a request |
| G9 | Two channels, one engine | Customer and business traffic differ only by profile — guardrails, output contract, audit sink, quota bucket, tool allow-list — never by pipeline |
| G10 | Cache before inferring | A semantic-cache hit costs no model call and no fan-out; hit rate and the spend it avoids are both reported |
| G11 | Full observability | Structured logs, Prometheus metrics, OTel traces; one trace per turn spanning every downstream, with per-stage latency and per-turn cost |
| G12 | Prompts and routing are versioned artifacts | Prompt registry with pinned versions; a prompt change is reviewable, canaryable and revertible like code |
| G13 | Precise documentation | API reference, event-stream reference, runbook |

## 3. Non-goals (v1)

- **No tool implementations.** air-orchestrator-service selects and calls; the work lives in
  `air-tools-gateway`. A capability added there must not require a code change here.
- **No mutating capability of its own.** air-orchestrator-service never writes to a business system
  directly, not even "just this once" for a simple case.
- **No retrieval or indexing.** Vector stores, chunking and embeddings are `air-rag-service`'s.
- **No model hosting or provider SDKs.** `air-model-gateway` is the only model path.
- **No model gateway of its own.** The diagram's *Model Gateway* box is `air-model-gateway`
  :8083 (which replaced air-llm), and it already does multi-provider routing by model class,
  fallback, cost policy and budgets. This
  repo consumes it. Building a second one is the failure mode this non-goal exists to prevent.
- **No API gateway.** Both edge gateways are infrastructure, not application code. air-orchestrator-service
  authenticates its own callers and derives the channel from the principal, but it does not
  terminate TLS, shed load, or hold the WAF.
- **No MCP server in v1.** Optional on the diagram, and a second path to tools when the first
  does not exist yet. [HLD §8](01-hld.md).
- **No UI.** `air-client`'s Chat tab covers exercising the API.
- **No business data ownership.** Sessions and turn transcripts only; orders, customers and
  catalogue stay behind the services that own them.
- **No token-level streaming in v1.** See §4. v1 shipped without it; it was added
  without a contract change once the gateway could stream (spec 0004).

## 4. Decisions taken

| Decision | Choice | Rationale |
| --- | --- | --- |
| Orchestration boundary | air-orchestrator-service decides *what* to call; the specialist services decide *how* | Keeps the orchestrator small and the capabilities independently deployable |
| Read/write split | One `air-tools-gateway`; `read` and `write` capabilities on separate endpoints and separately scoped keys; writes propose → confirm → execute | Per §1. Revised 2026-11-14 from two services (air-tools, air-action): see "Decided: one tools gateway" below |
| Customer identity | A short-lived customer token signed per tenant, sent beside the API key and verified here; its `sub` becomes the principal's subject (spec 0012 §9 Q1; verification is spec 0014) | A key identifies an application, not a shopper, and "my orders" needs a shopper. Never taken from a body or from the model. Decided 2026-11-14 |
| Tools gateway tenancy | One `read` and one `execute` key per tenant at air-tools-gateway; the tenant comes from the key, never from a request (spec 0012 §9 Q2) | A service key that may name any tenant makes one bug here a cross-tenant read or write. Matches air-rag-service. Decided 2026-11-14 |
| Mutation flow | **Propose → confirm → execute**, confirmation recorded on the session | A mutation triggered by a model's reading of free text needs a human "yes" in the loop that outlives the process |
| Streaming shape | **SSE, orchestration events, plus token deltas** — stage events, then the answer as provisional `answer.delta` events and an authoritative `answer` | v1 shipped stage events alone, because air-llm could not stream. air-model-gateway can, so deltas were added as the compatible later addition this row anticipated: a new event, not a new contract. They are guardrailed through a hold-back buffer, and the authoritative `answer` retracts them on a refusal (spec 0004; product decision 2026-11-14: stream on both channels) |
| Model access | Exclusively via air-model-gateway's `POST /v1/generate`, over HTTP (`clients/model_gateway.py`), asking for a model *class* (`cloud`), never a model | G7. Centralised routing, failover, budgets, cache and cost accounting are the whole point of the gateway. Reached directly, never through air-infra. Replaced air-llm's `/v1/inference` in spec 0003 |
| Channel model | One service, `customer` and `business` channels, derived from the **authenticated principal** | Two deployments would duplicate the orchestrator; one undifferentiated endpoint would apply the weaker profile to both. Deriving from a header would let the client pick its own profile |
| Guardrails | **Inside air-orchestrator-service, both directions** | Review item 01. A gateway filter sees a request, not a prompt, and cannot tell that a retrieved document is being read as an instruction |
| Semantic cache | Embedding-similarity cache, checked **before** classification and fan-out | Review item 04 puts hits at 20–40% of traffic. A hit that already paid for classification, retrieval and a model call has spent most of what it was meant to save |
| Cache isolation | Cache and session keys namespaced by tenant, always | A semantic cache is a cross-request read path; a shared namespace turns one tenant's answer into another's cache hit |
| Prompts | Versioned in a registry, pinned per route, canaryable | Review item 06. A prompt is production logic; an unversioned edit is an unreviewable deploy |
| Session state | Redis, obtained through air-infra's credential broker | Durable and replica-shared; `shopassist-service`'s in-process dicts are the anti-pattern being corrected. **In-process today** — the Redis backend is Phase 2b, behind the same `SessionStore` protocol |
| Downstream integration | Typed async HTTP clients per service, fail-soft, per-call budget | Most of the platform is unbuilt (§6); the orchestrator has to run against whatever subset is actually up |
| Language / runtime | Python 3.12, FastAPI, Pydantic v2 | Matches every other AIR service |
| Auth | `X-API-Key`, salted-sha256 key store, scopes | Same shape as air-classifier, so `air-client` and any consumer authenticate identically across services |
| Port | **8081** | Already reserved for air-orchestrator-service in air-infra's port map |
| Config | `pydantic-settings`, `AIR_ORCHESTRATOR_SERVICE__*`, `__` nesting | House convention: `AIR_<SERVICE>__` with `__` nesting, the prefix derived from the repo name |

### Resolved decisions (formerly "open questions")

Resolved against two real inputs, not just review discussion: `shopassist-service`
(`~/git/iisc-genai/shopassist-service`) — a working capstone predecessor whose retrospective
and known-gaps history this section cites directly — and an enterprise reference diagram the
user supplied for review. Where the two disagreed, shopassist's evidence won: it is proven,
running code, not an aspiration.

1. **Routing mechanism — resolved as LLM-based decomposition, reversing the earlier
   tool-calling recommendation.** `shopassist-service`'s `call_task_decomposer()` is exactly
   this shape, proven in production use, and the model gateway confirmed still has no `tools`
   field on its inference contract (air-llm then; air-model-gateway's `/v1/generate` now) (checked against its actual source, not assumed) — so
   tool-calling remains blocked on a change in a repo this one doesn't own. Decomposition needs
   nothing further to ship. Migrate to native tool-calling once air-model-gateway grows it; the tool list
   still assembles at runtime from each service's `/v1/capabilities`, so that migration is a
   planner-internal change, not a contract change.
2. ~~**Where PII masking belongs.**~~ *Resolved by the diagram — and re-confirmed against
   shopassist's actual code, not just its diagram.* Guardrails — injection defence, PII
   redaction, policy and safety filters — sit **inside air-orchestrator-service and run on both
   directions** (review item 01). The enterprise diagram reviewed alongside this decision
   places PII masking *before* the API gateway, ahead of authentication; that placement is
   **rejected** — even `shopassist-service` itself masks inside the orchestrator, after auth,
   never ahead of the gateway. `shopassist-service` masking inside the orchestrator was
   originally flagged as a placement to revisit; the correction is a guardrail stage at the
   platform edge (still behind auth), not a gateway filter, because only the platform can see
   the assembled prompt. Redaction is configurable on the business channel, where internal
   analysts legitimately query customer records — configurable, never absent.
3. **Confirmation lifetime — resolved.** Short TTL on the session, cancelled by any turn that
   does not answer it, so a stale "yes" cannot execute an hour-old proposal
   (`SessionSettings.proposal_ttl_seconds`). The confirmation match itself is a **deterministic
   keyword check on the next turn, never a second LLM call** — shopassist's own reasoning
   applies directly: routing a confirmation through an LLM would let a prompt injection talk
   its way into confirming its own proposal.
4. **Turn transcript retention — deferred, not resolved.** shopassist never needed a Postgres
   transcript table in practice; its retrospective names other gaps but never this one. Left
   out of v1 scope; revisit only if the business channel's audit log (review item 05) forces it.
5. **Semantic cache correctness — resolved as designed, plus one addition.** Never cache a turn
   whose answer depended on tool output, retrieval, or any per-user data; cache only the
   direct-answer route, with a high similarity threshold and an exact match on extracted
   entities. **Addition, adopted from the reference diagram:** a separate, cheap routing/intent
   cache (known intents/routes, TTL-based) sits *ahead* of this semantic answer cache — lower
   risk than the answer cache, since a wrong routing-cache hit costs an extra step rather than
   serving a wrong fact, and it stays useful even for turns the answer cache's eligibility rules
   exclude.
6. **`air-recommender`'s place — resolved.** Not on the diagram. A fourth read-path capability,
   same shape as an air-tools-gateway `read` capability, not architecturally distinct ([HLD §1](01-hld.md)).

### Decided: one tools gateway (2026-11-14)

**Supersedes the two-service split (air-tools read-only, air-action mutating) and, for tools,
the agent-placement decision below.**

*Decision.* One REST service, **`air-tools-gateway`** on :8084, fronts every external
business system for both lookups and changes. It advertises named capabilities, each `read`
or `write`, from `GET /v1/capabilities`; the orchestrator discovers them at runtime. The
gateway makes **no model call**. Choosing which capability a turn needs, and in what order,
stays in this service's planner. air-action (:8085) will not be built.

*Why.* Two services doubled the deployment, the contract and the on-call surface for a
platform at the size of this one, before either existed. The properties the split was meant
to guarantee do not depend on a repo boundary, and each now has an explicit owner:

| The split gave | Now carried by |
| --- | --- |
| A turn cannot write | Article II (unchanged): a write is proposed, and executes only on a later turn's structured `confirm` (spec 0001) |
| The read path holds no write credential | Two key scopes per tenant: a **read** key for inventory, lookups and propose, an **execute** key for execute only, refused everywhere else (spec 0012 `REQ-TGW-016`, contract rule 5) |
| air-action re-validates independently | The gateway's execute re-checks ownership, state and limits (contract rule 7) |
| Idempotent, audited writes | `Idempotency-Key` derived from the proposal id, never retried here; an `action.executed` audit line (`REQ-TGW-015`, `018`, `021`) |

*What is given up,* and accepted: a bug in a lookup handler now runs in a process that also
holds write credentials, and reads and writes no longer scale or deploy independently. If
either starts to matter, the gateway's `write` handlers can move to their own deployment
behind the same contract with no change here, because the endpoints and keys are already
separate.

*Why no model inside the gateway.* It keeps the gateway a plain REST service that a backend
developer can build and test deterministically, keeps all model traffic and its guardrails
in one place (this service), and means one planner sees the whole turn rather than an agent
in each service seeing a slice. A capability's `description` and `input_schema` are the only
part of the gateway a model reads, and they are reviewed here.

*Contract.* [`docs/specs/0012-tools-gateway/contract.openapi.yaml`](specs/0012-tools-gateway/contract.openapi.yaml),
written here and handed to the gateway's builders. Behaviour on this side is spec
[0012](specs/0012-tools-gateway/spec.md); the planner is spec 0013, to follow.

### New decision: agent placement inside air-tools/air-rag-service

> **Amended 2026-11-14 for tools:** superseded by "Decided: one tools gateway" above. The
> gateway hosts no agents and no model client, and `POST /v1/agents/{name}` will not exist;
> the planner here decomposes the turn. The decision still stands for air-rag-service.

`shopassist-service`'s five agents (`OrderTrackingAgent` et al.) each run **reason → tool call
→ interpret** — two LLM calls plus a data call, per agent, per sub-task. Externalising RAG and
tools into their own repos (`air-rag-service`, `air-tools`) forced a decision this section's original
scope didn't cover: where does that reasoning loop live once it's not all one process?

**Decided: inside air-tools and air-rag-service, not air-orchestrator-service.** Each hosts its own air-model-gateway client
and does its own reason/interpret calls; air-orchestrator-service sends **one coarse request per sub-task**
(`POST /v1/agents/{name}` on air-tools) and folds the structured result into synthesis. This
keeps the non-goal already on record — "a tool added there must not require a code change
here" — true for agent logic as well as flat capability calls, and matches the per-role
fine-tuned LLM endpoints on the reference diagram (`agent_reason`, `agent_interpret`), realised
as per-caller overrides in air-model-gateway's routing file rather than new services. air-tools' read-only boundary is
unchanged: an agent that finds a mutation warranted returns a description for air-orchestrator-service to
route to air-action, and never executes anything itself — the same structural gap that closes
the highest-severity issue in shopassist's own retrospective (no ownership/confirmation check
on `cancel_order`/`delete_order`).

## 5. Phased delivery

Each phase ends in a runnable, testable state. **Phases 0, 1 and 2a are shipped**;
[LLD §15](02-lld.md) is the authoritative per-phase record, and this section is the intent
behind it.

### Phase 0 — Skeleton · *shipped*

`pyproject.toml`, Makefile, Dockerfile, compose, `pydantic-settings` config, app factory with
lifespan, structured logging, request-context middleware, RFC 9457 errors, API-key auth,
`/v1/health` + `/v1/ready` + `/v1/capabilities`.
*Exit:* `make up` serves a healthy app on :8081 and `air-client`'s System tab reads it.

### Phase 1 — Contracts · *shipped*

Every Pydantic request/response model, the session model, and the **SSE event contract** for
both channels. `/v1/chat` and `/v1/query` stubbed with a scripted orchestrator that emits a
realistic event sequence. OpenAPI published.
*Exit:* `air-client`'s Chat tab drops its request-builder presets and codes against the real
contract; the reply extractor gets a real path instead of probing.

### Phase 2 — Orchestrator core + guardrails

Split in two, because the turn engine does not depend on the session backend:

**2a — the turn engine · *shipped*.** Turn lifecycle, guardrails in and out, prompt registry,
model calls through the model gateway (air-llm then; air-model-gateway since spec 0003), answer synthesis, real event stream, enforced turn and session
budgets.
*Exit, met:* A multi-turn conversation with **zero downstream services running**, and an
injection attempt that is caught rather than answered.

**2b — durable sessions · *not started*.** Move the session store onto air-infra's brokered
Redis behind the existing `SessionStore` protocol. The tenant-namespaced key shape is already
in place, so this is a backend swap.
*Exit:* Two replicas serve alternating turns of one conversation indistinguishably.

### Phase 3 — Read path · *not started*

Typed clients for `air-rag-service`, `air-tools-gateway` (`read` capabilities, spec 0012),
`air-classifier`, `air-recommender`; the planner (spec 0013); capability discovery; parallel fan-out with per-call budgets and fail-soft degradation; grounding and
citation checks in the output guardrail.
*Exit:* Answers grounded in retrieval and read-only tools; each service can be killed
individually without failing a turn.

### Phase 4 — Write path · *not started*

`air-tools-gateway`'s write path (spec 0012): propose → confirm → execute on the separate
execute key, confirmation state on the session,
idempotency keys derived from proposal ids, mutation audit records, async-queue state
surfaced as stage events.
*Exit:* A mutating request completes end to end, and is refused end to end when unconfirmed.

### Phase 5 — Semantic cache + business channel · *not started*

Embedding-similarity cache with tenant namespacing and the eligibility rules from §4 Q5;
the business channel's structured-output contract and schema validation; immutable audit log;
per-team quota and cost attribution.
*Exit:* Measured cache hit rate and avoided spend; a business query returns validated
structured output.

### Phase 6 — Evaluation, hardening & ops · *not started*

Offline eval suites, canary prompts, A/B prompt rollout, human-feedback capture; rate limits
and quotas, bulkheads on fan-out, per-turn cost ceiling, Prometheus metrics, OTel traces,
load test, runbook, image published.
*Exit:* Ready for a production pilot, with a prompt change gated by an eval run.

## 6. Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| **Prompt injection reaches a mutation.** Retrieved content or user text talks the model into proposing a destructive action | Unauthorised writes to a business system | The propose-only rule is the primary control: air-orchestrator-service can only ever *propose*, and execute needs a key the read path never holds. Beyond that — confirmation is a separate turn against a stored proposal, never inferable from the same message; the gateway re-validates at execute and is the last word; retrieved content and tool results are delimited and never treated as instruction |
| **Most of the platform does not exist yet.** air-tools-gateway is not built (air-rag-service now is) | Phases 3–4 have nothing to integrate against | Contract-first: define each client against the service's published contract, ship a fixture-backed fake, and make absence a capability downgrade (G5). Phase 2 is deliberately shaped to be useful with zero downstreams |
| **Fan-out latency compounds.** Three services plus a model, serially, blows the turn budget | Unusable P95 | Parallel fan-out with a per-turn deadline; partial results synthesise into an answer that says what is missing. Stage events mean the user sees progress even at the slow end |
| Cost blowout on a single turn | Unbounded spend from a runaway loop | Hard per-turn ceiling on model calls and tokens, enforced in the orchestrator and cross-checked against the gateway's own cost accounting; a turn that hits it answers with what it has |
| Session state grows unbounded | Redis memory exhaustion, and long histories inflate every prompt | TTL on every session key; a bounded turn window in the prompt with older context summarised |
| **Semantic cache returns a confidently wrong answer.** Two near-identical vectors are two different questions | Worse than a cache miss — a plausible answer about the wrong order | Cache only the direct-answer route; never cache anything grounded in tool output, retrieval or per-user data; high similarity threshold plus exact entity match; tenant-namespaced keys (§4 Q5) |
| **Cross-tenant leakage through a shared read path** — cache, session, or an unscoped downstream call | Confidentiality breach, and the hardest class of bug to detect after the fact | Tenant is on the request context from authentication, is part of every cache and session key, and is passed to every downstream. Tested as an explicit isolation suite, not assumed |
| Guardrails cause false refusals | Legitimate traffic blocked; the business channel is most exposed, since analyst queries look adversarial | Per-channel guardrail profiles; every block emits a structured event with the rule that fired, so refusals are measurable and tunable rather than anecdotal |
| The gateway becomes a single point of failure for chat | All conversation stops when air-model-gateway is down | It genuinely is one — accepted deliberately, since centralised cost/key custody is worth it. Mitigation is on air-model-gateway's side (provider failover), plus an honest degraded answer here rather than a hang |
| Confirmation UX is ambiguous over a stream | A user's "yes" applies to the wrong proposal | The proposal carries an id, the confirmation must cite it, and any turn that does not answer the proposal cancels it (§4 Q3) |

## 7. Success metrics

- **Latency:** first event < 250 ms, P95 final answer < 6 s on a read-path turn.
- **Availability:** 99.9% of turns answered; a degraded answer counts as answered.
- **Degradation:** with any one downstream killed, 100% of turns still answer.
- **Safety:** zero mutations without a recorded confirmation, and zero cross-tenant reads.
  Both are hard gates, not rates.
- **Cache:** ≥20% hit rate on eligible traffic (review item 04's floor), with avoided spend
  reported alongside it.
- **Cost:** measured per turn and attributed per stage from day one.

## 8. Review checklist

Status against each, as of this review round:

- [x] The diagram-to-repo mapping in [HLD §1](01-hld.md) — the *Model Gateway* box is
      **air-llm** :8083, not air-infra: air-llm split out of air-infra after this plan was
      first drafted, and air-infra now brokers only Redis/Postgres/Mongo and secrets. This
      correction is applied throughout the plan, HLD, LLD and code. *Intent & Sentiment*
      remains air-classifier. **Since superseded:** air-model-gateway replaced air-llm in the
      same slot (spec 0003, constitution 1.1.0).
- [x] Two channels on one engine, with the channel derived from the principal (HLD §3)
- [x] The read/write split as stated in §1, and G3's "enforced by the client layer" — and now
      independently evidenced: shopassist-service's retrospective names skipping exactly this
      check as its highest-severity gap
- [x] Propose → confirm → execute as the only mutation path, confirmation by deterministic
      keyword match on the next turn, never a second LLM call (§4 item 3)
- [x] **SSE stage events for v1, with token-by-token deferred to an air-llm change** —
      a recorded deviation from the diagram's *Response Streamer* (HLD §5). **Closed
      2026-11-14:** air-model-gateway streams, and `answer.delta` is emitted (spec 0004)
- [ ] Sessions on brokered Redis rather than in-process — **Phase 2b, not started.** The
      `SessionStore` protocol and tenant-namespaced key shape exist so this is a backend
      swap, not a redesign
- [x] Semantic cache placed before classification and fan-out, with the eligibility rules in
      §4 item 5, plus a routing/intent cache ahead of it (adopted from the reference diagram).
      Designed, not built — Phase 5
- [x] The phase ordering — specifically Phase 2 landing before any downstream integration.
      Held: Phase 2a shipped a real turn with zero downstream services running
- [x] The six items in §4 are resolved (item 4, transcript retention, deferred rather than
      decided) — **Q1 (routing mechanism) is resolved as LLM-based decomposition**, reversing
      the earlier tool-calling recommendation, since the gateway still has no `tools` field
- [x] **2026-11-14:** one `air-tools-gateway` replaces air-tools and air-action, with separate
      read and execute keys and no model inside it (§4 "Decided: one tools gateway", spec 0012)
- [x] **New:** agent placement — reason/tool/interpret loops live inside air-tools/air-rag-service,
      each with its own air-model-gateway client; air-orchestrator-service sends one coarse request per sub-task.
      **Amended 2026-11-14:** no longer for tools; see the gateway item above
