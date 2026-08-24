# AIR Platform — Plan

**Status:** Draft for review · **Owner:** Vikas Roy · **Date:** 2026-08-17
**Companion docs:** [HLD](01-hld.md) · [LLD](02-lld.md)
**Source:** *AIR-PLATFORM — High-Level Architecture, Rev A*, and its independent-review
items 01–08. The HLD reconciles that diagram against the AIR repositories.

---

## 1. Problem statement

`air-platform` is the AIR platform's **conversational front door**. A client sends a message;
air-platform decides what that message needs, gathers it from the specialist services, and
streams back a grounded reply.

Everything expensive or specialised already lives somewhere else — retrieval in `air-rag`,
classification in `air-classifier`, recommendations in `air-recommender`, reads in
`air-tools`, writes in `air-action`, models and stores behind `air-infra`. What does *not*
exist is the thing that turns "where is my order and can you cancel it" into a sequence of
calls across those services and one coherent answer.

Three constraints define the design:

**The read/write split is architectural, not stylistic.** `air-tools` performs read-only
calls and nothing else. `air-action` performs mutating calls, and each one passes external
checks before it commits. This split is why the two are separate services rather than one
tool registry with a flag: a boundary the orchestrator cannot accidentally cross is worth
more than a boolean it must remember to check. A model that has been talked into calling
`cancel_order` still has to get that call past air-action, and air-platform's job is to
make sure such a call is *proposed* explicitly, confirmed, and audited — never a silent
side effect of a retrieval turn.

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
| G1 | One conversational API for the whole platform | A client integrates against air-platform alone and reaches every AIR capability |
| G2 | Streaming turn lifecycle | Client renders the first event well before the final answer; every stage is observable as it completes |
| G3 | Strict read/write separation | Every mutation goes through air-action; air-platform holds no mutating capability of its own, and this is enforced by the client layer, not by convention |
| G4 | Confirmed, auditable mutations | No mutating call executes without an explicit confirmation step recorded against the session |
| G5 | Degrades service by service | Any downstream being absent removes a capability from the turn; it never fails the turn |
| G6 | Durable multi-turn sessions | Conversation state survives restart and is shared across replicas, via air-infra's brokered Redis |
| G7 | All model traffic through air-infra | No provider SDK and no provider key in this repo; cost and cache stay centralised |
| G8 | Guardrails on both directions, inside the platform | Injection defence, PII redaction and grounding checks run on input *and* output, where the prompt is visible — not at the gateway, which only sees a request |
| G9 | Two channels, one engine | Customer and business traffic differ only by profile — guardrails, output contract, audit sink, quota bucket, tool allow-list — never by pipeline |
| G10 | Cache before inferring | A semantic-cache hit costs no model call and no fan-out; hit rate and the spend it avoids are both reported |
| G11 | Full observability | Structured logs, Prometheus metrics, OTel traces; one trace per turn spanning every downstream, with per-stage latency and per-turn cost |
| G12 | Prompts and routing are versioned artifacts | Prompt registry with pinned versions; a prompt change is reviewable, canaryable and revertible like code |
| G13 | Precise documentation | API reference, event-stream reference, runbook |

## 3. Non-goals (v1)

- **No tool implementations.** air-platform selects and calls; the work lives in `air-tools`
  and `air-action`. A tool added there must not require a code change here.
- **No mutating capability of its own.** air-platform never writes to a business system
  directly, not even "just this once" for a simple case.
- **No retrieval or indexing.** Vector stores, chunking and embeddings are `air-rag`'s.
- **No model hosting or provider SDKs.** `air-infra`'s gateway is the only model path.
- **No model gateway of its own.** The diagram's *Model Gateway* box is `air-infra` :8080,
  which already does multi-provider routing, fallback, cost policy and token budgets. This
  repo consumes it. Building a second one is the failure mode this non-goal exists to prevent.
- **No API gateway.** Both edge gateways are infrastructure, not application code. air-platform
  authenticates its own callers and derives the channel from the principal, but it does not
  terminate TLS, shed load, or hold the WAF.
- **No MCP server in v1.** Optional on the diagram, and a second path to tools when the first
  does not exist yet. [HLD §8](01-hld.md).
- **No UI.** `air-client`'s Chat tab covers exercising the API.
- **No business data ownership.** Sessions and turn transcripts only; orders, customers and
  catalogue stay behind the services that own them.
- **No token-level streaming in v1.** See §4 — the gateway cannot stream today, and the
  chosen event contract does not require it to.

## 4. Decisions taken

| Decision | Choice | Rationale |
| --- | --- | --- |
| Orchestration boundary | air-platform decides *what* to call; the specialist services decide *how* | Keeps the orchestrator small and the capabilities independently deployable |
| Read/write split | `air-tools` read-only, `air-action` mutating behind external checks | A structural boundary, per §1. Also lets the two scale, fail and get audited differently |
| Mutation flow | **Propose → confirm → execute**, confirmation recorded on the session | A mutation triggered by a model's reading of free text needs a human "yes" in the loop that outlives the process |
| Streaming shape | **SSE, orchestration events** — stage events plus the final answer as one chunk | air-infra's gateway returns complete responses ([`/v1/models/chat`](../../air-infra/src/air_infra/api/v1/models.py)); the `stream` flag on its `ChatRequest` is unimplemented. Stage events deliver the responsiveness that matters without blocking on a two-repo change. Token deltas remain a compatible later addition — a new event type, not a new contract |
| Model access | Exclusively via `air_infra_client.models` | G7. Centralised routing, failover, cache and cost accounting are the whole point of the gateway |
| Channel model | One service, `customer` and `business` channels, derived from the **authenticated principal** | Two deployments would duplicate the orchestrator; one undifferentiated endpoint would apply the weaker profile to both. Deriving from a header would let the client pick its own profile |
| Guardrails | **Inside air-platform, both directions** | Review item 01. A gateway filter sees a request, not a prompt, and cannot tell that a retrieved document is being read as an instruction |
| Semantic cache | Embedding-similarity cache, checked **before** classification and fan-out | Review item 04 puts hits at 20–40% of traffic. A hit that already paid for classification, retrieval and a model call has spent most of what it was meant to save |
| Cache isolation | Cache and session keys namespaced by tenant, always | A semantic cache is a cross-request read path; a shared namespace turns one tenant's answer into another's cache hit |
| Prompts | Versioned in a registry, pinned per route, canaryable | Review item 06. A prompt is production logic; an unversioned edit is an unreviewable deploy |
| Session state | Redis, obtained through air-infra's credential broker | Durable and replica-shared; `shopassist-service`'s in-process dicts are the anti-pattern being corrected |
| Downstream integration | Typed async HTTP clients per service, fail-soft, per-call budget | Most of the platform is unbuilt (§6); the orchestrator has to run against whatever subset is actually up |
| Language / runtime | Python 3.12, FastAPI, Pydantic v2 | Matches every other AIR service |
| Auth | `X-API-Key`, salted-sha256 key store, scopes | Same shape as air-classifier, so `air-client` and any consumer authenticate identically across services |
| Port | **8081** | Already reserved for air-platform in air-infra's port map |
| Config | `pydantic-settings`, `AIR_PLATFORM__*`, `__` nesting | House convention |

### Resolved decisions (formerly "open questions")

Resolved against two real inputs, not just review discussion: `shopassist-service`
(`~/git/iisc-genai/shopassist-service`) — a working capstone predecessor whose retrospective
and known-gaps history this section cites directly — and an enterprise reference diagram the
user supplied for review. Where the two disagreed, shopassist's evidence won: it is proven,
running code, not an aspiration.

1. **Routing mechanism — resolved as LLM-based decomposition, reversing the earlier
   tool-calling recommendation.** `shopassist-service`'s `call_task_decomposer()` is exactly
   this shape, proven in production use, and air-llm confirmed still has no `tools` field on
   its unified inference contract (checked against its actual source, not assumed) — so
   tool-calling remains blocked on a change in a repo this one doesn't own. Decomposition needs
   nothing further to ship. Migrate to native tool-calling once air-llm grows it; the tool list
   still assembles at runtime from each service's `/v1/capabilities`, so that migration is a
   planner-internal change, not a contract change.
2. ~~**Where PII masking belongs.**~~ *Resolved by the diagram — and re-confirmed against
   shopassist's actual code, not just its diagram.* Guardrails — injection defence, PII
   redaction, policy and safety filters — sit **inside air-platform and run on both
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
   same shape as air-tools, not architecturally distinct ([HLD §1](01-hld.md)).

### New decision: agent placement inside air-tools/air-rag

`shopassist-service`'s five agents (`OrderTrackingAgent` et al.) each run **reason → tool call
→ interpret** — two LLM calls plus a data call, per agent, per sub-task. Externalising RAG and
tools into their own repos (`air-rag`, `air-tools`) forced a decision this section's original
scope didn't cover: where does that reasoning loop live once it's not all one process?

**Decided: inside air-tools and air-rag, not air-platform.** Each hosts its own air-llm client
and does its own reason/interpret calls; air-platform sends **one coarse request per sub-task**
(`POST /v1/agents/{name}` on air-tools) and folds the structured result into synthesis. This
keeps the non-goal already on record — "a tool added there must not require a code change
here" — true for agent logic as well as flat capability calls, and matches the per-role
fine-tuned LLM endpoints on the reference diagram (`agent_reason`, `agent_interpret`), realised
as air-llm `routing.yaml` aliases rather than new services. air-tools' read-only boundary is
unchanged: an agent that finds a mutation warranted returns a description for air-platform to
route to air-action, and never executes anything itself — the same structural gap that closes
the highest-severity issue in shopassist's own retrospective (no ownership/confirmation check
on `cancel_order`/`delete_order`).

## 5. Phased delivery

Each phase ends in a runnable, testable state.

### Phase 0 — Skeleton

`pyproject.toml`, Makefile, Dockerfile, compose, `pydantic-settings` config, app factory with
lifespan, structured logging, request-context middleware, RFC 9457 errors, API-key auth,
`/v1/health` + `/v1/ready` + `/v1/capabilities`.
*Exit:* `make up` serves a healthy app on :8081 and `air-client`'s System tab reads it.

### Phase 1 — Contracts

Every Pydantic request/response model, the session model, and the **SSE event contract** for
both channels. `/v1/chat` and `/v1/query` stubbed with a scripted orchestrator that emits a
realistic event sequence. OpenAPI published.
*Exit:* `air-client`'s Chat tab drops its request-builder presets and codes against the real
contract; the reply extractor gets a real path instead of probing.

### Phase 2 — Orchestrator core + guardrails

Turn lifecycle, guardrails in and out, session store on brokered Redis, prompt registry,
routing, model calls through the air-infra gateway, answer synthesis, real event stream.
*Exit:* A grounded multi-turn conversation with **zero downstream services running**, and an
injection attempt that is caught rather than answered.

### Phase 3 — Read path

Typed clients for `air-rag`, `air-tools`, `air-classifier`, `air-recommender`; capability
discovery; parallel fan-out with per-call budgets and fail-soft degradation; grounding and
citation checks in the output guardrail.
*Exit:* Answers grounded in retrieval and read-only tools; each service can be killed
individually without failing a turn.

### Phase 4 — Write path

`air-action` client, propose → confirm → execute, confirmation state on the session,
idempotency keys derived from proposal ids, mutation audit records, async-queue state
surfaced as stage events.
*Exit:* A mutating request completes end to end, and is refused end to end when unconfirmed.

### Phase 5 — Semantic cache + business channel

Embedding-similarity cache with tenant namespacing and the eligibility rules from §4 Q5;
the business channel's structured-output contract and schema validation; immutable audit log;
per-team quota and cost attribution.
*Exit:* Measured cache hit rate and avoided spend; a business query returns validated
structured output.

### Phase 6 — Evaluation, hardening & ops

Offline eval suites, canary prompts, A/B prompt rollout, human-feedback capture; rate limits
and quotas, bulkheads on fan-out, per-turn cost ceiling, Prometheus metrics, OTel traces,
load test, runbook, image published.
*Exit:* Ready for a production pilot, with a prompt change gated by an eval run.

## 6. Risks

| Risk | Impact | Mitigation |
| --- | --- | --- |
| **Prompt injection reaches a mutation.** Retrieved content or user text talks the model into proposing a destructive action | Unauthorised writes to a business system | The split itself is the primary control: air-platform can only ever *propose*. Beyond that — confirmation is a separate turn against a stored proposal, never inferable from the same message; air-action re-validates independently and is the last word; retrieved content is delimited and never treated as instruction |
| **Most of the platform does not exist yet.** air-tools, air-action, air-rag are empty repos | Phases 3–4 have nothing to integrate against | Contract-first: define each client against the service's published contract, ship a fixture-backed fake, and make absence a capability downgrade (G5). Phase 2 is deliberately shaped to be useful with zero downstreams |
| **Fan-out latency compounds.** Three services plus a model, serially, blows the turn budget | Unusable P95 | Parallel fan-out with a per-turn deadline; partial results synthesise into an answer that says what is missing. Stage events mean the user sees progress even at the slow end |
| Cost blowout on a single turn | Unbounded spend from a runaway loop | Hard per-turn ceiling on model calls and tokens, enforced in the orchestrator and cross-checked against the gateway's own cost accounting; a turn that hits it answers with what it has |
| Session state grows unbounded | Redis memory exhaustion, and long histories inflate every prompt | TTL on every session key; a bounded turn window in the prompt with older context summarised |
| **Semantic cache returns a confidently wrong answer.** Two near-identical vectors are two different questions | Worse than a cache miss — a plausible answer about the wrong order | Cache only the direct-answer route; never cache anything grounded in tool output, retrieval or per-user data; high similarity threshold plus exact entity match; tenant-namespaced keys (§4 Q5) |
| **Cross-tenant leakage through a shared read path** — cache, session, or an unscoped downstream call | Confidentiality breach, and the hardest class of bug to detect after the fact | Tenant is on the request context from authentication, is part of every cache and session key, and is passed to every downstream. Tested as an explicit isolation suite, not assumed |
| Guardrails cause false refusals | Legitimate traffic blocked; the business channel is most exposed, since analyst queries look adversarial | Per-channel guardrail profiles; every block emits a structured event with the rule that fired, so refusals are measurable and tunable rather than anecdotal |
| The gateway becomes a single point of failure for chat | All conversation stops when air-infra is down | It genuinely is one — accepted deliberately, since centralised cost/key custody is worth it. Mitigation is on air-infra's side (provider failover) plus a clean, honest error here rather than a hang |
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

- [x] The diagram-to-repo mapping in [HLD §1](01-hld.md) — corrected: the *Model Gateway* box
      is **air-llm**, not air-infra (air-llm split out of air-infra after this plan was first
      drafted; air-infra now brokers only Redis/Postgres/Mongo and secrets — see the README's
      "Doc debt" note for what in this doc still needs the same correction). *Intent &
      Sentiment* remains air-classifier.
- [x] Two channels on one engine, with the channel derived from the principal (HLD §3)
- [x] The read/write split as stated in §1, and G3's "enforced by the client layer" — and now
      independently evidenced: shopassist-service's retrospective names skipping exactly this
      check as its highest-severity gap
- [x] Propose → confirm → execute as the only mutation path, confirmation by deterministic
      keyword match on the next turn, never a second LLM call (§4 item 3)
- [ ] **SSE stage events for v1, with token-by-token deferred to an air-llm change** —
      this is a recorded deviation from the diagram's *Response Streamer* (HLD §5)
- [ ] Sessions on brokered Redis rather than in-process — still Phase 2 work, not started
- [x] Semantic cache placed before classification and fan-out, with the eligibility rules in
      §4 item 5, plus a routing/intent cache ahead of it (adopted from the reference diagram)
- [ ] The phase ordering — specifically Phase 2 landing before any downstream integration
- [x] The six items in §4 are resolved (item 4, transcript retention, deferred rather than
      decided) — **Q1 (routing mechanism) is resolved as LLM-based decomposition**, reversing
      the earlier tool-calling recommendation, since air-llm still has no `tools` field
- [x] **New:** agent placement — reason/tool/interpret loops live inside air-tools/air-rag,
      each with its own air-llm client; air-platform sends one coarse request per sub-task
