# 0013 — The planner: choosing capabilities for a turn

**Status:** Implemented · **Area:** `PLN` · **Phase:** 3 · **Updated:** 2026-11-15
**Derives from:** [Plan §4 resolved item 1 (LLM-based decomposition)](../../00-plan.md) · [HLD §4 step 6, §9 item 2](../../01-hld.md) ·
spec [0012](../0012-tools-gateway/spec.md) (the `Planner` seam, inventory, steps) · spec [0008](../0008-turn-cost-ceiling/spec.md) (cost) ·
spec [0006](../0006-channel-prompts/spec.md) (versioned prompts)
**Supersedes:** — (retires the `/propose` stand-in trigger once enabled, `REQ-PLN-010`)

## 1. Problem

Spec 0012 lets a turn call air-tools-gateway capabilities, but only from a `Plan`, and the
only planner is `RulePlanner`, which never emits a gateway step (Article V). So nothing
decides that "where is my order?" needs `orders.list_recent`, or that "cancel my last order"
needs a lookup and then a proposal. Routing is still a rule plus the literal `/propose`. The
decided mechanism (Plan §4) is decomposition by a model: one structured-output call through
air-model-gateway, because `/v1/generate` has `json_schema` and no `tools` field.

## 2. Outcome

On a customer turn that is offered at least one capability, a model call turns the message,
the recent conversation and the offered capabilities into a validated `Plan`: lookups to run,
and at most one change to propose. When a proposal depends on what a lookup returns ("my
*last* order"), a second, smaller planning call sees the lookup results and fills in the
proposal. Spec 0012's checks then run on the plan exactly as on any other: allow-list,
schema, subject, business-channel writes. A planner that fails, times out or returns garbage
costs the turn its lookups, not its answer. The planner's prompt is a versioned artifact with
its own evaluation set. Once it is enabled, `/propose` is only text.

## 3. Scope

**In scope**

- When the planner runs, and when it is skipped without a model call.
- What it is shown, and what it must never be shown.
- The plan's output schema, its validation, and the fallback.
- The two-round shape for dependent steps.
- Its budget (deadline share, cost), prompt versioning, evaluation set, observability.
- Retiring the `/propose` stand-in.

**Out of scope**

- **Native tool-calling.** It waits for air-model-gateway (HLD §9 item 2). The `Planner`
  protocol is the seam it will replace behind.
- **Deciding retrieval by model.** Retrieval stays a rule (spec 0011; §9 Q3; `REQ-PLN-015`): the planner's schema has no retrieval field.
- **The business channel.** `business_analyst` turns stay direct in v1: no capability is
  offered there yet, because its allow-list is empty (`REQ-TGW-004`). This spec applies there
  unchanged once one is.
- **Having the model word proposal and confirmation answers.** They stay canned (spec 0012
  plan §1).
- **Acting on the classifier's verdict** beyond showing it to the planner (§9 Q2: no
  skipping in v1).

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| I — The channel comes from the credential | The planner chooses among capabilities | It sees only the capabilities spec 0012 already filtered for the principal's channel; its output is filtered again (`REQ-PLN-006`) |
| II — Mutations are proposed, never performed | The planner can ask for a change | It can only add a `propose` step; nothing it emits reaches execute, which needs the structured confirmation of a later turn |
| III — A missing downstream narrows the answer | The planner depends on air-model-gateway | A failed, slow or invalid plan falls back to the rule plan, and the turn answers (`REQ-PLN-004`, `005`) |
| IV — Tenant-namespaced | The planner sees capability lists | Only the turn's tenant's inventory, and never the tenant, subject or keys (`REQ-PLN-002`) |
| V — A stage that cannot run says so | `PLAN` has always reported `ok` for a rule | `PLAN` reports what ran: the model plan, the rule (skipped, with why), or degraded (`REQ-PLN-011`). `/propose` stops being a trigger (`REQ-PLN-010`) |
| VI — Public contracts are closed sets | Route event, stage detail | No new names. `route.routes` uses existing `Route` members; `route.capabilities` follows `REQ-TGW-025` |
| VII — All model traffic through air-model-gateway | A new model call | Through `ModelGatewayClient.generate` with `json_schema`, asking for a model class |
| IX — Secrets never render | Prompt content | No key, token, subject or tenant enters the planner prompt (`REQ-PLN-002`) |
| X — Proved by a test | Forward spec | Every row names its test |

## 5. Requirements

**When it runs**

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-PLN-001` | MUST | A turn is planned by one model call through air-model-gateway with a `json_schema` when it is not a confirmation turn, the planner is enabled, and spec 0012 offers it at least one capability. Whether the turn proposes a change is the planner's output, not a precondition. Otherwise the turn uses the rule plan and makes no planning call. |
| `REQ-PLN-002` | MUST | The planner prompt contains exactly: the input-masked message; the session's last `history_turns` turns, masked as stored; the classifier's intent and sentiment when present; and the offered capabilities' `name`, `kind`, `description` and `input_schema` inside one `delimit(..., source="capabilities")` block. It MUST NOT contain the subject, tenant, any key or token, or any capability not offered to this turn. |

**What it returns**

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-PLN-003` | MUST | The planner's output is a JSON object validated against the plan schema: `reads`, a list of at most `max_reads` (3) `{capability, arguments}`; `propose`, one `{capability, arguments}` or null; `needs_results`, a boolean; and `reason`, a string, cut to 200 characters when longer (it is only logged, as a hash and a length, and a provider may ignore `maxLength`: Gemini wrote 216 in SIT and lost a correct plan). Anything else is invalid. The schema sent with the call pairs each offered capability's name with its own `input_schema` as the shape of its `arguments`, so a provider that constrains decoding to the schema (Gemini fills an unconstrained object with nothing) is told which arguments each step takes; spec 0012's checks still validate them afterwards. |
| `REQ-PLN-004` | MUST | An invalid plan (unparseable, schema-invalid, or a refusal from the gateway) falls back to the rule plan. `PLAN` reports `degraded` naming `air-model-gateway`, and the turn still answers. |
| `REQ-PLN-005` | MUST | Each planning call is bounded by `deadline_share` (0.3) of the turn deadline and by what is left of it, and its estimated cost counts toward the turn and session ceilings (spec 0008). A call that would not fit, or that times out, falls back as `REQ-PLN-004`. |
| `REQ-PLN-006` | MUST | Every step in a plan passes spec 0012's checks before anything is sent (`REQ-TGW-004`, `006`, `020`, `026`). A plan cannot widen what was offered: a step naming anything else is dropped and reported. |

**Dependent steps**

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-PLN-007` | MUST | When a round-1 plan sets `needs_results` and carries at least one read, a second planning call is made after those reads return. It sees the round-1 plan and the read results, screened and delimited as `GATHER` puts them in the answer prompt. It may return only a `propose` step or none: no further reads. There is never a third round. |
| `REQ-PLN-008` | MUST NOT | A `propose` step's arguments come from anywhere but the planner's output, validated against the capability's schema. In particular, an identifier the round-2 planner copies from a lookup result is accepted only if it validates, and the gateway still checks ownership at propose and at execute. |

**Prompt and evaluation**

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-PLN-009` | MUST | The planner prompt is a versioned registry entry (`planner:v2` since 2026-11-15; `planner:v1` stays pinnable), and its version is logged with every planned turn. A change is a new version, pinnable like the channel prompts. An evaluation set under `evals/planner/` of message-to-expected-plan cases is run by `make planner-eval` (live, paid, not in `make check`) before and after a change. |
| `REQ-PLN-016` | MUST | The planner prompt instructs that a change the customer asks for on something they identify, in the message or the conversation, is proposed in round 1, beside the reads for the rest of the message; that `needs_results` is set only when the change's identifier has to come from a lookup; that round 2 may take the identifier from the customer's message or the conversation as well as from the results; and that only a capability making exactly the change asked for is proposed (a refund is not a cancellation). (From `planner:v2`, 2026-11-15, OR-014a: under v1, "Cancel order 55120. Also, where is 48213?" read 48213 only, deferred the cancel to round 2, and round 2, told to copy identifiers from the results, proposed nothing.) A round-1 proposal with reads is answered as `REQ-TGW-032` says. |
| `REQ-PLN-017` | MUST | When round 1 falls back per `REQ-PLN-004` or `005` on a turn that was offered capabilities, the answer prompt carries spec 0012's could-not-look-up notice for the turn's order and account lookups and the change-not-prepared notice of §7, so the answer neither says a lookup or change is under way nor asks the customer to confirm one. (OR-015, 2026-11-15: with the planner timing out after a gateway restart and no notice, Gemini asked "Could you please confirm that you wish to cancel order 55120?" with no proposal made, and said "The system is checking on order 70011 for you now.") The turn still answers, so a greeting or policy question is answered as usual. |

**The stand-in**

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-PLN-010` | MUST | With the planner enabled, the literal `/propose` in a message has no effect: no stand-in `echo.noop` proposal is made. With it disabled, the rule plan keeps the stand-in, so the gate stays exercisable without a model. |

**Observability**

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-PLN-011` | MUST | `PLAN` reports round 1: `ok` with `reads=M propose=yes/no needs_results=yes/no` for a model plan; `skipped` with the reason for a rule plan (`planner disabled`, `no capabilities offered`, `no capabilities offered: air-tools-gateway inventory unavailable` naming `air-tools-gateway` as its downstream, `nothing to plan: a confirmation turn`); or `degraded` per `REQ-PLN-004`. Round 2 runs after `GATHER` has started, and stage events keep `Stage` order, so it is reported in `GATHER`'s detail (`round 2: propose <capability>` or `round 2: no change`, or its failure; `round 2: skipped: no lookup results` when no read returned anything to show it), never as a second `PLAN` event. |
| `REQ-PLN-012` | MUST | Each planning call is counted in `air_orchestrator_service_planner_calls_total{round,outcome}`, outcome one of `ok`, `invalid`, `timeout`, `error`, `skipped_budget`. |
| `REQ-PLN-013` | MUST NOT | The planner's raw output, its `reason`, or the capability catalogue reaches the client. The client sees the `route` event (`REQ-TGW-025`) and stage details only. |
| `REQ-PLN-014` | MUST | The planner asks air-model-gateway for its own model class, `planner.model_class`, defaulting to `cloud` and independent of the synthesis class. |
| `REQ-PLN-015` | MUST | A model plan with reads also retrieves from air-rag-service, as spec 0011 decides for every customer turn that is synthesised, so a message that asks about an order and a policy at once gets both. Beside lookups the knowledge base is supplementary: when it fails or the deadline is too close, the turn is not `degraded` for it, the prompt is not told it was unavailable, and `GATHER`'s detail says `knowledge base not consulted: <reason>`. A plan with a proposal does not retrieve (its turn is fixed text). |

## 6. Acceptance criteria

**AC-1 — An order question becomes a lookup** (`REQ-PLN-001`, `003`)
- **Given** a customer with a subject, `orders.*` offered, and a scripted planner response `{"reads":[{"capability":"orders.list_recent","arguments":{"limit":5}}],"propose":null,"needs_results":false,"reason":"…"}`
- **When** the customer asks "where is my order?"
- **Then** one planning call is made with a `json_schema`, `orders.list_recent` is called, and `PLAN` reports `ok reads=1 propose=no needs_results=no`

**AC-2 — "My last order" takes two rounds** (`REQ-PLN-007`, `008`)
- **Given** round 1 returns `orders.list_recent {"limit":1}` with `needs_results: true`, and the lookup returns order 55120
- **When** round 2 returns `propose orders.cancel {"order_id":"55120"}`
- **Then** exactly two planning calls are made; the second's prompt carries the delimited result; one propose request is sent for 55120; `GATHER`'s detail says `round 2: propose orders.cancel`; no third call is made

**AC-3 — Garbage costs the lookups, not the answer** (`REQ-PLN-004`)
- **Given** the planning call returns `"sure! here's a plan:"`, or times out
- **When** the turn runs
- **Then** no gateway call is made, `PLAN` is `degraded` naming `air-model-gateway`, and the turn answers from retrieval and the model as it would without tools

**AC-4 — The planner cannot widen the menu** (`REQ-PLN-006`)
- **Given** only `orders.get` is offered
- **When** the plan names `orders.list_recent` and `payments.refund`
- **Then** neither is called, both are named in the stage detail, and the turn answers

**AC-5 — The prompt holds no identity** (`REQ-PLN-002`)
- **Given** a principal with subject `cust_1001`, tenant `brightcart`, and keys configured
- **When** the planner prompt is captured
- **Then** none of `cust_1001`, `brightcart`, either key or the customer token appears in it, and only offered capabilities do

**AC-6 — `/propose` is just text** (`REQ-PLN-010`)
- **Given** the planner enabled
- **When** the message is `/propose`
- **Then** no `proposal` event is emitted

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| Planner disabled | Rule plan, no planning call; `PLAN` skipped "planner disabled" | `ok` |
| No capabilities offered (gateway down, no subject, empty allow-list) | Rule plan, no planning call | as the rest of the turn |
| Planning call fails, times out, or is invalid | Rule plan; `PLAN` degraded naming `air-model-gateway`; the answer is told no lookup was made and no change prepared (`REQ-PLN-017`) | `degraded` |
| Round 2 fails | Round-1 reads are kept and answered from; no proposal; the answer says it could not prepare the change | `degraded` |
| Planning would breach the cost ceiling | Rule plan; counted `skipped_budget` | as spec 0008 |

## 8. Observability

- `PLAN` stage detail (`REQ-PLN-011`) and `air_orchestrator_service_planner_calls_total{round,outcome}` (`REQ-PLN-012`).
- The prompt version (`planner:v2`), logged with the turn, beside the channel prompt's version.
- Planner tokens and estimated cost in the turn's `usage`, attributed as planning, so the
  extra call's cost is visible per turn.

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| Q1 | ~~Which model class plans?~~ **Decided 2026-11-14** — see below. | — | Product owner |
| Q2 | ~~Should the classifier's intent skip planning?~~ **Decided 2026-11-14** — see below. | — | Product owner |
| Q3 | ~~Should the planner decide whether to retrieve?~~ **Decided 2026-11-14** — see below. | — | Product owner |
| Q4 | ~~Are `max_reads` 3 and two rounds enough?~~ **Decided 2026-11-14** — see below. | — | Product owner |

### Decisions taken

**Q1 — Model class: a setting of its own, defaulting to `cloud`** (`REQ-PLN-014`). A plan is
short structured output, so a cheaper class may well do. That is decided by `make
planner-eval` scores, not by assumption, and changing it is then a settings change.

**Q2 — No skipping on intent in v1.** The classifier's intent and sentiment are shown to the
planner (`REQ-PLN-002`). Nothing is skipped on them, because a wrong `small_talk` verdict
would silently hide a lookup. Measure first: the planner metric and the classifier verdict
together show how often an intent would have predicted "no capability needed".

**Q3 — Retrieval stays a rule.** Customer turns keep retrieving as spec 0011 decides. The
planner's schema has no retrieval field. Revisit when retrieval cost or latency matters.
*Revisited 2026-11-15 (product owner):* a plan with lookups or a proposal no longer
retrieves (`REQ-PLN-015`). In SIT a knowledge-base outage degraded order questions the
lookup had fully answered, and an order question gains nothing from policy passages.
*Revisited 2026-11-16 (product owner, platform SIT):* a plan with lookups retrieves again,
because "my recent orders, and how long do refunds take?" lost its policy half ("the exact
terms are in your order confirmation"). The outage concern is kept another way: beside
lookups a knowledge-base failure no longer degrades the turn (`REQ-PLN-015`).

**Q4 — Three reads, two rounds.** Enough for "compare my last two orders" (two reads, one
round) and "cancel whichever hasn't shipped" (round 2). Both limits are settings
(`max_reads`, fixed at 2 rounds), so raising `max_reads` needs no spec change. A third round
does.

## 10. Traceability

> Every row proved. `REQ-PLN-001` and `011` are also proved by `tests/unit/test_planner.py::test_a_confirmation_turn_is_not_planned`; `REQ-PLN-009`'s and `016`'s evaluation half by `tests/unit/test_planner_eval.py` and the `change-plus-question` and `override-other-customer` cases.

| Id | Proof |
| --- | --- |
| `REQ-PLN-001` | `tests/unit/test_planner.py::test_a_turn_with_capabilities_is_planned_by_one_structured_call` |
| `REQ-PLN-002` | `tests/unit/test_planner.py::test_the_planner_prompt_holds_no_identity_and_only_offered_capabilities` |
| `REQ-PLN-003` | `tests/unit/test_planner.py::test_the_plan_is_validated_against_its_schema` |
| `REQ-PLN-004` | `tests/unit/test_planner.py::test_an_invalid_plan_falls_back_and_the_turn_answers` |
| `REQ-PLN-005` | `tests/unit/test_planner.py::test_planning_is_bounded_and_counted_against_the_ceiling` |
| `REQ-PLN-006` | `tests/unit/test_planner.py::test_the_planner_cannot_widen_the_offered_capabilities` |
| `REQ-PLN-007` | `tests/unit/test_planner.py::test_a_dependent_proposal_takes_exactly_two_rounds` |
| `REQ-PLN-008` | `tests/unit/test_planner.py::test_round_two_arguments_must_validate` |
| `REQ-PLN-009` | `tests/unit/test_prompts.py::test_the_planner_prompt_is_versioned_and_logged` |
| `REQ-PLN-010` | `tests/unit/test_planner.py::test_the_propose_trigger_is_inert_when_the_planner_is_enabled` |
| `REQ-PLN-011` | `tests/unit/test_planner.py::test_the_plan_stage_reports_what_ran` with `tests/unit/test_planner.py::test_an_unavailable_inventory_is_named_in_the_plan_stage` |
| `REQ-PLN-012` | `tests/unit/test_planner.py::test_every_planning_call_is_counted` |
| `REQ-PLN-013` | `tests/unit/test_planner.py::test_the_plan_never_reaches_the_client` |
| `REQ-PLN-014` | `tests/unit/test_planner.py::test_the_planner_uses_its_own_model_class` |
| `REQ-PLN-015` | `tests/unit/test_planner.py::test_a_plan_with_lookups_also_retrieves`, `tests/unit/test_planner.py::test_a_knowledge_base_failure_does_not_degrade_a_turn_with_lookups` |
| `REQ-PLN-016` | `tests/unit/test_prompts.py::test_the_planner_prompt_proposes_a_named_change_in_round_one` |
| `REQ-PLN-017` | `tests/unit/test_planner.py::test_a_failed_plan_tells_the_answer_nothing_was_looked_up_or_prepared` |
