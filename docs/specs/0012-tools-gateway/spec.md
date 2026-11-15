# 0012 — air-tools-gateway: lookups and proposed mutations through one gateway

**Status:** Implemented · **Area:** `TGW` · **Phase:** 3 (read) / 4 (write) · **Updated:** 2026-11-15
**Derives from:** [Plan §1, §4 "Decided: one tools gateway"](../../00-plan.md) · [HLD §1, §4, §6, §7](../../01-hld.md) ·
spec [0001](../0001-mutation-gate/spec.md) (the gate) · spec [0010](../0010-downstream-client-base/spec.md) (client base) ·
spec [0011](../0011-retrieval-grounding/spec.md) (how downstream text enters a prompt)
**Supersedes:** — (replaces the *design* of separate air-tools and air-action services; no
earlier spec governed either)
**Contract:** [`contract.openapi.yaml`](contract.openapi.yaml) — the gateway's API, written
here and handed to its builders

## 1. Problem

A customer can ask "where is my order?" or "cancel my last order", and nothing in this
service can look an order up or change one. `GATHER` reaches air-rag-service only. The action
route is a stand-in: the literal `/propose` produces an `echo.noop` proposal with arguments
"nothing executes", and a confirmed proposal answers "Executed echo.noop (no air-action client
exists yet — nothing actually changed)". The plan called for two services, a read-only
air-tools and a mutating air-action. Neither exists, and the platform has decided to build one
REST gateway in their place (Plan §4). The safety properties the two-service split carried
now have to be stated as requirements, because a repo boundary no longer carries them for free.

## 2. Outcome

The orchestrator learns what air-tools-gateway can do from the gateway's
`GET /v1/capabilities` at runtime. Each capability is a named, schema-described operation of
kind `read` or `write`. A turn may call `read` capabilities in `GATHER`, concurrently and within
its deadline, and their results reach the synthesis prompt as delimited data. A `write`
capability is only ever *proposed* in the turn that wants it. It executes on a later turn that
carries the structured confirmation spec 0001 already requires, through a separate execute
key that no other request carries, with an idempotency key, and it is never retried. The
caller sees the orchestrator's own proposal id, never the gateway's. When the gateway is down,
its capabilities leave the turn, and the answer says what it could not look up or do. The
gateway makes no model call: deciding *which* capability a turn needs stays in this service's
planner (spec 0013, to follow).

## 3. Scope

**In scope**

- Capability discovery: fetching, validating and filtering the gateway's inventory.
- Calling `read` capabilities in `GATHER` and putting their results in the prompt.
- Proposing `write` capabilities, holding the gateway's proposal on the session, and
  executing it on confirmation.
- The two keys (read and execute), tenant scoping, and the customer identity envelope.
- Degradation, observability, and the wire label for the new service.
- The contract the gateway implements (`contract.openapi.yaml`), enforced here by an
  integration double.

**Out of scope**

- **Choosing capabilities.** The planner, a schema-constrained model call that turns a
  message into steps ("look up the latest order, then propose cancelling it"), is spec 0013.
  Until it lands, `/propose` stays the stand-in trigger (Article V), and this spec's
  acceptance tests drive the engine with an injected plan.
- **Verifying the customer token.** Today a customer-channel key identifies an application and
  a tenant, not a shopper. §9 Q1 decides where the identity comes from: a short-lived customer
  token, signed per tenant. Verifying it and putting its subject on the principal is spec 0014,
  to follow. This spec defines how the subject, once present, travels to the gateway and what
  is withheld without one (`REQ-TGW-008`).
- **The gateway's implementation.** It is a Node service built against the contract; its
  backends, storage and tests are its own.
- **Agents inside the gateway.** The gateway has no model client. This reverses the plan's
  earlier agent-placement decision for tools (Plan §4). air-rag-service is unaffected.
- **air-recommender.** It stays a separate read-path client.
- **Approval-queue polling or push.** A high-risk execution may come back
  `pending_approval`; the customer checks later, through a `read` capability (§9 Q6).

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| I — The channel comes from the credential | Channel decides the tool allow-list | The allow-list is per channel in settings, applied to the principal's channel; nothing in a request can widen it; the business channel gets no writes at all (`REQ-TGW-004`, `026`) |
| II — Mutations are proposed, never performed | One service now holds both reads and writes | A proposing turn makes no execute request; execution needs the spec 0001 confirmation, a separate execute key and an idempotency key (`REQ-TGW-012`, `015`, `016`). Article II never named air-action, so it needs **no amendment** |
| III — A missing downstream narrows the answer | The gateway may be down, slow, or missing a capability | Its capabilities leave the turn; `GATHER` degrades naming it; the answer says what could not be checked (`REQ-TGW-003`, `010`, `011`) |
| IV — Every key is tenant-namespaced | Order data is per tenant and per customer | Each tenant has its own read/execute key pair at the gateway (§9 Q2); a tenant without one gets no capabilities; a proposal is bound to the key and subject that made it (`REQ-TGW-007`, `019`) |
| V — A stage that cannot run says so | No planner yet | `/propose` stays a stand-in until spec 0013; `TOOLS` is not chosen by a rule that pretends to understand intent |
| VI — Public contracts are closed sets | `DownstreamService.TOOLS` / `.ACTION` (`air-tools`, `air-action`) are wire and metric labels | Replaced by `air-tools-gateway` on purpose (`REQ-TGW-023`). `Route.TOOLS` and `Route.ACTION` stay as route vocabulary |
| VII — All model traffic goes through air-model-gateway | The gateway makes no model call | Nothing to route; the planner's call is this service's, through air-model-gateway |
| VIII — Nothing is a module-level singleton | New client, inventory cache, breaker | All live on `AppState`; a test builds a fresh app with its own |
| IX — Secrets never render | Two keys per tenant now | Neither appears in a log, repr, error or response (`REQ-TGW-016`) |
| X — A claim is proved by a test | Forward spec | Every requirement names the test that will prove it, as `DEFERRED` until the code exists |

## 5. Requirements

**Discovery**

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-TGW-001` | MUST | The capabilities a turn may use come from the gateway's `GET /v1/capabilities`. A capability added there, inside a namespace the channel's allow-list already admits, becomes usable with no code change and no configuration change here. The gateway answers per key, so the inventory is held per tenant, and one tenant's inventory never serves another's turn. |
| `REQ-TGW-002` | MUST | An inventory entry is dropped, logged once by name, and the rest kept, when any of these fails: the name matches `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$` and is at most 64 characters; `kind` is `read` or `write`; `input_schema` is a JSON Schema object with `additionalProperties: false` that declares no property named `subject` or `tenant` and no top-level `patternProperties`, which could admit either through a pattern (identity travels in the envelope, `REQ-TGW-008`); the description is at most 1000 characters; a `write` entry's `risk` is `low`, `medium` or `high`. At most 64 entries are kept. |
| `REQ-TGW-003` | MUST | The inventory is refetched at most once per `inventory_ttl_s` (60 s). After a failed refresh the last good inventory keeps serving for at most `inventory_grace_s` (300 s) from its fetch; past that, or with none, the turn has no gateway capabilities. A failed fetch never fails the turn. |
| `REQ-TGW-004` | MUST NOT | A capability that the principal's channel allow-list does not admit is offered to the planner, called, or proposed. The allow-list is per channel, in settings, as capability names and `namespace.*` patterns. The default for both channels is empty. |
| `REQ-TGW-005` | MUST | `/v1/capabilities` lists the `tools` route only while the gateway is reachable and offers the caller's channel at least one `read` capability, and the `action` route only for a `write` capability on the same terms. |

**Read calls**

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-TGW-006` | MUST NOT | A read request is sent for a name that is not a `read` capability in the current inventory, or with arguments that do not validate against its `input_schema`. Such a step is dropped and reported in the `GATHER` stage detail. |
| `REQ-TGW-007` | MUST | Every gateway request carries a key from the read/execute pair mapped to the principal's tenant; each gateway key belongs to exactly one tenant and one scope, so the gateway derives the tenant from the key and no request names one. A tenant with no mapped pair gets no gateway capabilities. With no map configured, one pair serves every tenant (single-tenant deployments only, as `REQ-RAG-016`). |
| `REQ-TGW-008` | MUST | The customer identity (`subject`) in a request comes only from the authenticated principal, which takes it from a verified customer token (§9 Q1, spec 0014): never from planner output, a capability argument, the message, or an unverified header. A capability that declares `requires_subject: true` is not offered to a turn whose principal has no verified subject. |
| `REQ-TGW-009` | MUST | A read result reaches the synthesis prompt delimited as data (`boundary.delimit`), after the injection screen, cut to `max_result_chars`. A result that fails the screen is dropped, and the stage detail says so. |
| `REQ-TGW-010` | MUST | One turn's read calls run concurrently. Each is bounded by the client's deadline share and by the time the turn has left. Any failure reports `GATHER` as `degraded` naming `air-tools-gateway`, and the turn still answers. A refused read (`REQ-TGW-029`) is not a failure. |
| `REQ-TGW-011` | MUST | When a planned read fails or is dropped, the synthesis prompt carries a notice naming what could not be looked up, so the answer can say so instead of guessing. |

**Write path**

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-TGW-012` | MUST NOT | A turn that proposes a `write` capability sends any request but the gateway's propose request for it. No execute request is sent on a turn that does not carry a confirmation resolved under spec 0001. |
| `REQ-TGW-013` | MUST | The gateway's `proposal_id` is held only on the server-side pending proposal. The caller sees only this service's own `prop_` id, in the `proposal` event and everywhere else. |
| `REQ-TGW-014` | MUST | The published proposal's `summary` and `risk` come from the gateway's propose response, and its `expires_at` is the earlier of the gateway's and `session.proposal_ttl_seconds`. A propose response with a risk outside `low`/`medium`/`high` produces no proposal. |
| `REQ-TGW-015` | MUST | An execute request carries an `Idempotency-Key` derived from this service's proposal id, so that the same proposal always produces the same key. |
| `REQ-TGW-016` | MUST | The execute request alone carries the tenant's **execute** key. Inventory, read and propose requests carry the **read** key and never the execute key. Neither key appears in a log line, repr, error message or response. |
| `REQ-TGW-017` | MUST | The answer to a confirmed turn follows the execute outcome. `done` says it was done. `pending_approval` says it is awaiting approval, is not yet done, and that the customer can ask again later to check. `rejected` says it was not done, worded from the gateway's closed-set `code`, never its free text. A timeout, unreachable gateway or error says the outcome is **unknown**, and never that it was done. An open circuit, which sends no request at all, says it was **not done** and may be tried again later. An execute the gateway answers with `404 not_found` or `409 proposal_expired` acted on nothing, so it also says **not done**, and that the change can be asked for afresh; it is the gateway answering, not failing, so it does not count against the circuit. `409 already_executed` and any other refusal stay **unknown**, because the change may already have been made. |
| `REQ-TGW-018` | MUST NOT | An execute request is retried, automatically or on a later turn, by this service. |
| `REQ-TGW-019` | MUST | A pending proposal records the `key_id` and `subject` that created it. A confirmation from a principal with a different `key_id` or `subject` executes nothing and answers normally, as `REQ-MUT-008`. |
| `REQ-TGW-020` | MUST NOT | A propose request is sent for a principal whose key record sets `allow_actions: false`. This closes the gap recorded against `REQ-MUT-012` before any real mutation is reachable. |
| `REQ-TGW-021` | MUST | Every execution writes one `action.executed` audit log line with the proposal id, capability, outcome, `key_id` and tenant, and no argument values. |

**Observability and contract**

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-TGW-022` | MUST | Every capability call is counted in `air_orchestrator_service_capability_calls_total{capability,kind,outcome}`. The label's cardinality is bounded by `REQ-TGW-002`. |
| `REQ-TGW-023` | MUST | The gateway appears in API responses and metric labels as `DownstreamService.TOOLS_GATEWAY = "air-tools-gateway"`. `air-tools` and `air-action` are removed from `DownstreamService`, as a deliberate Article VI change: neither service was ever deployed. |
| `REQ-TGW-024` | MUST | The integration double for the gateway enforces `contract.openapi.yaml`'s request shapes (no unknown fields) and key scopes (the read key is refused on execute, and the execute key everywhere else), so a request the real gateway would refuse fails the suite. |
| `REQ-TGW-025` | MUST NOT | The `route` event lists any capability the turn's plan did not use. The inventory is reconnaissance, which `/v1/capabilities` already gates behind `admin:read`; a customer-channel event stream must not hand it to every caller. |
| `REQ-TGW-026` | MUST NOT | A `write` capability is offered to the planner or proposed on the business channel, whatever its allow-list admits. |
| `REQ-TGW-027` | MUST | The rules for using lookup results — state facts exactly as given and nothing they lack, use each date only for what its field names (an order date is never a cancellation date; a status carries no date of its own, so asked when, the answer says it doesn't have that date — OR-014b, where Gemini gave the placed date as the cancellation date under the field-name rule alone), never ask for a detail the results already show (when the customer did not say which order, name the ones shown and ask which), say when a block is partial, never mention the mechanism, and say it could not check when a lookup failed — travel in the lookups message, which is present only on a turn whose plan had lookups. The channel prompt is not changed for them, so a turn without lookups is synthesised with the same prompt as before this spec. A rule that applies only when lookups are present then costs nothing on a turn without them, and that turn needs no new prompt review. |
| `REQ-TGW-028` | MUST | A propose answered with a problem whose `code` is `not_found`, `invalid_state`, `not_permitted` or `limit_exceeded` is a refusal, not a failure: it does not count toward the breaker, it is recorded with outcome `refused` on both call metrics, the turn status is `ok`, and the answer begins with the fixed text for that code, which names no cause the code does not (`invalid_state` says the order's current state, never "for example once it has shipped"), and goes on as `REQ-TGW-033`. Any other non-2xx, or another code, is a failure as spec 0010 defines it. |
| `REQ-TGW-029` | MUST | A read answered with a problem whose `code` is `not_found` or `not_permitted` is a refusal, not a failure: it does not count toward the breaker, it is recorded with outcome `refused`, `GATHER` stays `ok` (detail `<name> found nothing`) and the turn status is `ok`. The synthesis prompt says that nothing was found on this customer's account for it, and not that the lookup failed or whether the record exists elsewhere. One customer's wrong order numbers must not switch lookups off for anyone else. |
| `REQ-TGW-030` | MUST | `options.allow_routes` narrows the turn before planning, never widens it: without `tools` the planner is offered no `read`, without `action` no `write` and no `/propose` stand-in, and without `rag` there is no retrieval. A confirmation turn's execution is a structured request, not planning, and is not narrowed. |
| `REQ-TGW-031` | MUST | When the gateway is configured for the turn's tenant and channel and its inventory is unavailable (the fetch failed past its grace, the circuit is open, or no time was left to fetch it), `GATHER` is `degraded` naming `air-tools-gateway`, with detail `air-tools-gateway is unavailable`, on every turn, planned reads or not, and the synthesis prompt carries the could-not-look-up notice. With the planner on, nothing can be planned from an empty menu, so a rule keyed on the plan would never report the outage. A turn whose `allow_routes` excludes both `tools` and `action` is not degraded by it. |
| `REQ-TGW-032` | MUST | A turn that proposes a change while its plan also had reads answers with the fixed proposal text followed by one fixed line saying the rest of the message will be answered once the change is confirmed or declined. The reads are not sent: a proposing turn's answer calls no model (`REQ-TGW-028`'s fixed-text rule), so nothing would use their results. A turn that proposes with no reads says nothing more. |
| `REQ-TGW-033` | MUST | When a turn's planned change is not proposed (refused per `REQ-TGW-028`, failed, or dropped), the rest of the message is still answered: the plan's reads are sent, and the answer is synthesised with a notice that the customer has already been told the change could not be prepared, so the model does not mention the change at all. The notice does not quote the fixed text: shown it, Gemini repeated it word for word. The answer is that fixed text followed by the synthesised text, and a streaming caller receives the fixed text as the first `answer.delta`. If synthesis fails, is refused or would breach a ceiling, the answer is the fixed text alone. A proposing turn's reads stay unsent (`REQ-TGW-032`), so a proposal is only ever answered by fixed text. (OR-016b, 2026-11-15: "Cancel order 55120. Also, where is 48213?" with 55120 already cancelled answered the refusal alone, three times, and dropped 48213.) |

## 6. Acceptance criteria

**AC-1 — A new capability needs no release here** (`REQ-TGW-001`)
- **Given** the customer allow-list admits `orders.*`, and the gateway starts advertising `orders.track`
- **When** the inventory next refreshes
- **Then** `orders.track` is offered to the planner, with no code or settings change

**AC-2 — The planner cannot reach outside the inventory** (`REQ-TGW-004`, `006`)
- **Given** an injected plan naming `payments.refund_all`, which the gateway does not advertise, and `orders.list_recent` with `{"limit": 500}`, which breaks its schema's `maximum`
- **When** the turn runs `GATHER`
- **Then** no request is sent for either, and the stage detail names both as dropped

**AC-3 — Whose orders is never the model's choice** (`REQ-TGW-008`)
- **Given** a principal with subject `cust_123`, and a plan whose arguments include `"subject": "cust_999"` and whose message says "show orders for cust_999"
- **When** `orders.list_recent` is called
- **Then** no request carries `cust_999`. The step with the stray argument is dropped, because no schema that passes `REQ-TGW-002` admits an argument named `subject`. The same step without it is sent with `subject` `cust_123`

**AC-4 — A proposing turn changes nothing** (`REQ-TGW-012`, `013`)
- **Given** a plan to propose `orders.cancel` for order 55120
- **When** the turn runs
- **Then** the gateway receives one propose request and no execute request, the `proposal` event carries a `prop_` id, and the gateway's own id appears nowhere in the response

**AC-5 — Only a confirmation executes, and only once** (`REQ-TGW-015`, `016`, `018`)
- **Given** a pending proposal for `orders.cancel`
- **When** a turn confirms it with `approve: true`, and the gateway times out
- **Then** exactly one execute request was sent, with the execute key and an `Idempotency-Key` derived from the `prop_` id; the answer says the outcome is unknown; and a second identical confirmation sends nothing (`REQ-MUT-006`)

**AC-6 — Prose still is not consent** (`REQ-TGW-012`, `REQ-MUT-004`)
- **Given** a pending proposal for `orders.cancel`
- **When** the next turn says "yes cancel it now" with no `confirm` field
- **Then** no execute request is sent, and the proposal is cancelled

**AC-7 — A proposal belongs to whoever made it** (`REQ-TGW-019`)
- **Given** a proposal created by key `k_web` for subject `cust_123`
- **When** a turn on the same tenant and session confirms it with key `k_other`, or with subject `cust_456`
- **Then** nothing executes, and the answer is indistinguishable from confirming an unknown id

**AC-8 — Unknown is never reported as done** (`REQ-TGW-017`)
- **Given** a confirmed proposal
- **When** execute returns `pending_approval`, or `rejected` with code `invalid_state`, or fails with a 503
- **Then** the answer says awaiting approval, not done because of its state, or outcome unknown, respectively, and none of them says "cancelled"
- **And when** execute returns `404 not_found` or `409 proposal_expired`, the answer says not done; with `409 already_executed` it says outcome unknown

**AC-9 — The gateway down narrows the turn** (`REQ-TGW-003`, `005`, `010`, `011`)
- **Given** the gateway is unreachable
- **When** `/v1/capabilities` is read, and a turn with a planned `orders.list_recent` runs
- **Then** neither `tools` nor `action` is listed; the turn answers; `GATHER` is `degraded` naming `air-tools-gateway`; and the prompt carries the "could not look up your orders" notice

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| Gateway not configured | No capabilities; `tools`/`action` routes absent; `/propose` stand-in unchanged | `ok` |
| Inventory fetch fails, or circuit open | No capabilities this turn; `GATHER` detail says the inventory is unavailable | `degraded` (`air-tools-gateway`) on every turn it would have served (`REQ-TGW-031`) |
| Inventory entry malformed | That entry dropped and logged; others served | `ok` |
| A read times out, fails, or its result fails the screen | Result dropped; notice in the prompt; other reads still used | `degraded` (`air-tools-gateway`) |
| Propose refused (`not_found`, `invalid_state`, `not_permitted`, `limit_exceeded`) | No proposal; the answer explains it from the code, then answers the rest of the message (`REQ-TGW-033`) | `ok` |
| Propose fails (timeout, unavailable, error) | No proposal; the answer says it could not prepare the change now, then answers the rest (`REQ-TGW-033`) | `degraded` (`air-tools-gateway`) |
| Execute `rejected` | Answer says not done, worded from the code | `ok` |
| Execute times out or fails | Answer says the outcome is unknown and how to check; never retried | `degraded` (`air-tools-gateway`) |
| Tenant has no gateway key | No capabilities, as if unconfigured, for that tenant only | `ok` |

## 8. Observability

- `air_orchestrator_service_downstream_calls_total{service="air-tools-gateway",outcome}` from
  the client base (spec 0010), and the breaker transitions it already logs.
- `air_orchestrator_service_capability_calls_total{capability,kind,outcome}` (`REQ-TGW-022`).
- The existing `record_proposal` outcomes, unchanged (`REQ-MUT-013`).
- An `action.executed` audit log line per execution (`REQ-TGW-021`).
- The `route` event's `capabilities` lists the capability names the plan used, and nothing
  else (`REQ-TGW-025`); `GATHER`'s stage detail names dropped steps and the reason.
- No argument value, result body or subject is logged unmasked (Article IX).

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| Q1 | ~~How does a customer-channel principal get a verified `subject`?~~ **Decided 2026-11-14** — see below. | — | Product owner |
| Q2 | ~~One read/execute key pair per tenant, or one service key with the tenant in the envelope?~~ **Decided 2026-11-14** — see below. | — | Product owner |
| Q3 | ~~Inventory freshness~~ **Decided 2026-11-14** — see below. | — | Product owner |
| Q4 | ~~Allow-list shape~~ **Decided 2026-11-14** — see below. | — | Product owner |
| Q5 | ~~Business-channel writes~~ **Decided 2026-11-14** — see below. | — | Product owner |
| Q6 | ~~`pending_approval` follow-through~~ **Decided 2026-11-14** — see below. | — | Product owner |

### Decisions taken

**Q1 — Customer identity: a short-lived customer token, signed per tenant.** The storefront
(or air-web-console in development) signs in the shopper, then mints a token whose `sub` is
the tenant's customer id, signed with that tenant's key, valid for minutes, and with this
service as its audience. The caller sends it beside its API key on each turn. This service
verifies signature, issuer, audience and expiry, and checks that the token's tenant is the
API key's tenant, before the subject reaches the principal. A missing, invalid or expired
token leaves the turn without a subject: it still answers, and per-customer capabilities are
not offered (`REQ-TGW-008`). The subject is never taken from a request body or from the
model. Token format, verification and key distribution are spec 0014. The gateway is not
affected: it receives `subject` in the request envelope from a caller its tenant key already
authenticates, and it enforces ownership with it.
*Why:* a key identifies an application, not a person, and "my orders" needs a person. A
signed, short-lived token is the only form that a stolen chat transcript or a crafted message
cannot forge or replay for long.

**Q2 — Tenant scoping: one read/execute key pair per tenant at the gateway.** Each gateway
key belongs to one tenant and one scope (`read` or `execute`), and the gateway derives the
tenant from the key. No request names a tenant. This service maps its tenants to their pairs
in settings, as `RagSettings.tenant_keys` does for air-rag-service.
*Why:* a single service key that may name any tenant in the body turns one bug in this
service into a cross-tenant read or write. Per-tenant keys make that impossible at the
gateway, and match how air-rag-service already scopes.

**Q3 — Inventory freshness: 60 s TTL, 5 minutes' grace.** Refetched at most every 60 s per
tenant. A failed refresh keeps the last good inventory for up to 5 minutes from when it was
fetched, so a brief gateway blip does not hide every capability. A call against a capability
that has really gone still fails soft (`REQ-TGW-010`).

**Q4 — Allow-list: per channel, in settings.** Names and `namespace.*` patterns, one list per
channel. This is Article I's "channel determines the tool allow-list" taken literally. A
per-key narrowing can be added later without changing this rule.

**Q5 — No writes on the business channel** (`REQ-TGW-026`). `/v1/query` may use `read`
capabilities only. Revisit when an internal use case for a change appears.

**Q6 — `pending_approval`: the customer asks later.** The answer says the change is awaiting
approval and can be checked later. A later turn's plan looks it up with a `read` capability
(`orders.get`). No push channel. v1 has no `high`-risk capability, so this path is exercised
only by tests.

## 10. Traceability

> Every requirement is proved by the named test, against the contract double
> (`REQ-TGW-024`), and the double's flows pass against air-tools-gateway's own build too
> (`make sit-tools`, tasks.md T11).
> No production turn reaches a lookup or a change until spec 0013's planner plans one.

| Id | Proof |
| --- | --- |
| `REQ-TGW-001` | `tests/unit/test_tools_inventory.py::test_a_new_capability_in_an_allowed_namespace_needs_no_change` |
| `REQ-TGW-002` | `tests/unit/test_tools_inventory.py::test_a_malformed_entry_is_dropped_and_the_rest_kept` |
| `REQ-TGW-003` | `tests/unit/test_tools_inventory.py::test_a_failed_fetch_leaves_no_capabilities_and_the_turn_answers` (the cache and grace) with `tests/unit/test_tools_stage.py::test_the_gateway_down_degrades_gather_and_the_turn_answers` (the turn) |
| `REQ-TGW-004` | `tests/unit/test_tools_stage.py::test_a_capability_outside_the_channel_allow_list_is_never_called` |
| `REQ-TGW-005` | `tests/unit/test_system_routes.py::test_capabilities_list_tools_and_action_only_when_the_gateway_offers_them` |
| `REQ-TGW-006` | `tests/unit/test_tools_stage.py::test_an_unknown_name_or_invalid_arguments_are_never_sent` |
| `REQ-TGW-007` | `tests/unit/test_tools_gateway_client.py::test_each_tenant_is_called_with_its_own_key` |
| `REQ-TGW-008` | `tests/unit/test_tools_stage.py::test_the_subject_comes_only_from_the_principal` with `tests/unit/test_tools_stage.py::test_a_capability_that_needs_a_customer_is_not_offered_without_one`. The principal is injected with a subject; a verified token supplies it from spec 0014 |
| `REQ-TGW-009` | `tests/unit/test_tools_stage.py::test_a_result_is_screened_delimited_and_cut` |
| `REQ-TGW-010` | `tests/unit/test_tools_stage.py::test_reads_run_concurrently_and_a_failure_degrades_gather` |
| `REQ-TGW-011` | `tests/unit/test_tools_stage.py::test_a_failed_read_puts_a_notice_in_the_prompt` |
| `REQ-TGW-012` | `tests/unit/test_action_execution.py::test_a_proposing_turn_sends_no_execute_request` |
| `REQ-TGW-013` | `tests/unit/test_action_execution.py::test_the_gateway_proposal_id_never_reaches_the_caller` |
| `REQ-TGW-014` | `tests/unit/test_action_execution.py::test_the_proposal_takes_the_earlier_expiry_and_a_known_risk` |
| `REQ-TGW-015` | `tests/unit/test_action_execution.py::test_the_idempotency_key_is_derived_from_the_proposal_id` |
| `REQ-TGW-016` | `tests/unit/test_tools_gateway_client.py::test_only_the_execute_request_carries_the_execute_key` |
| `REQ-TGW-017` | `tests/unit/test_action_execution.py::test_the_answer_follows_the_execute_outcome` |
| `REQ-TGW-018` | `tests/unit/test_action_execution.py::test_an_execute_failure_is_never_retried` |
| `REQ-TGW-019` | `tests/unit/test_action_execution.py::test_a_confirmation_from_another_key_or_subject_executes_nothing` |
| `REQ-TGW-020` | `tests/unit/test_action_execution.py::test_a_key_without_allow_actions_sends_no_propose_request` |
| `REQ-TGW-021` | `tests/unit/test_action_execution.py::test_an_execution_writes_one_audit_line_without_arguments` |
| `REQ-TGW-022` | `tests/unit/test_tools_gateway_client.py::test_every_capability_call_is_counted` |
| `REQ-TGW-023` | `tests/unit/test_system_routes.py::test_the_gateway_is_labelled_air_tools_gateway` |
| `REQ-TGW-024` | `tests/integration/test_tools_gateway_contract.py::test_the_double_refuses_what_the_contract_refuses` |
| `REQ-TGW-025` | `tests/unit/test_tools_stage.py::test_the_route_event_names_only_the_capabilities_used` |
| `REQ-TGW-026` | `tests/unit/test_action_execution.py::test_the_business_channel_is_never_offered_a_write` |
| `REQ-TGW-027` | `tests/unit/test_tools_stage.py::test_the_rules_for_lookups_travel_only_with_lookups` with `tests/unit/test_prompts.py::test_the_lookups_message_carries_the_rules_for_using_them` |
| `REQ-TGW-028` | `tests/unit/test_action_execution.py::test_a_refused_proposal_is_an_answer_not_a_failure` with `tests/unit/test_client_base.py::test_a_refusal_code_is_an_answer_and_nothing_else_is` |
| `REQ-TGW-029` | `tests/unit/test_tools_stage.py::test_a_refused_read_is_an_answer_and_keeps_the_breaker_closed` |
| `REQ-TGW-030` | `tests/unit/test_tools_stage.py::test_allow_routes_narrows_what_the_turn_may_use` |
| `REQ-TGW-031` | `tests/unit/test_tools_stage.py::test_an_unavailable_gateway_degrades_every_turn` |
| `REQ-TGW-032` | `tests/unit/test_action_execution.py::test_a_proposing_turn_says_the_rest_will_be_answered` |
| `REQ-TGW-033` | `tests/unit/test_action_execution.py::test_a_change_not_proposed_still_answers_the_rest` |
