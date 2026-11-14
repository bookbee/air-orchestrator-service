# 0012 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Ready · **Updated:** 2026-11-14

> Written provisionally while §9 Q3–Q6 were open, then settled when they were decided
> (2026-11-14): 60 s TTL with 5 minutes' grace, a per-channel allow-list in settings, no
> writes on the business channel, and "ask later" for `pending_approval`.

## 1. Approach

**A planner seam first, so this spec does not have to invent intent.** Today the route is
decided inline in `TurnEngine._pipeline` (the `/propose` check and the rag rule). That block
moves behind a `Planner` protocol in a new `engine/plan.py`. A `Plan` holds the routes, the
reason, the `read` steps to run, and at most one `write` step to propose. Each step is
`(capability, arguments)`. `RulePlanner` reproduces today's decision exactly and **never
emits a gateway step**, so with no planner change, nothing in this spec is reachable from a
real turn (Article V). Spec 0013 adds the model planner behind the same protocol. Tests
inject a `FixedPlanner(plan)`, which is the "injected plan" spec §3 promises.

**Two clients, two keys, two breakers.** `clients/tools_gateway.py` holds two
`DownstreamClient` subclasses for the one service:

- `ToolsGatewayClient`: `inventory(tenant)`, `run(...)` and `propose(...)`, on the tenant's
  **read** key. It has no execute-shaped method. It is the one in `optional_clients()`, so it
  is the one `/v1/capabilities` probes.
- `ActionExecutor`: `execute(...)` only, on the tenant's **execute** key. It is reachable
  only from the confirmation branch of the engine.

They are separate objects so the split is a property of the module surface (LLD §8). They
also get separate breakers, so a run of failed lookups cannot refuse a confirmed execute, and
a failed execute cannot take lookups down. Both share the `air-tools-gateway` label.

**The inventory.** `inventory(tenant)` calls `GET /v1/capabilities` with that tenant's read
key and caches the result per tenant for `inventory_ttl_s`. After a failed refresh it keeps
serving the last good one until `inventory_grace_s` from its fetch (`REQ-TGW-003`). Every entry is checked
against `REQ-TGW-002`: name pattern, kind, `input_schema` passing
`Draft202012Validator.check_schema` with `additionalProperties: false` and no
`subject`/`tenant` property, description length, write risk. A failing entry is dropped with
one `tools.inventory.entry_dropped` log line naming it. A valid entry keeps a compiled
validator. The inventory a turn sees is that list filtered by the channel's allow-list
(`REQ-TGW-004`), with every `write` entry removed on the business channel (`REQ-TGW-026`), and
by `requires_subject` against `principal.subject`.

**`GATHER`.** `_gather` keeps its single stage event. It now runs air-rag-service retrieval
and the plan's read steps concurrently (`asyncio.gather`). Each step is checked first:
- the name must be in the turn's inventory as `read`;
- the arguments must validate;
- otherwise it is dropped with a reason in the detail and a notice (`REQ-TGW-006`, `011`).

Each surviving call is bounded by `bound_s()` (`REQ-TGW-010`) and runs with the principal's
subject in the envelope (`REQ-TGW-008`). Each result is serialised (orjson, sorted keys),
screened with `screen_input()`, and cut to `max_result_chars` (`REQ-TGW-009`). Survivors
become a second system message, `_lookups_message()`, built like `_sources_message()`: one
`delimit(..., source="tool_result")` block per capability, plus a fixed
"could not look up: <capability>" notice line for each failed or dropped step. The stage
detail joins the parts, e.g. `sources=2 dropped=0; orders.list_recent ok; orders.get timed out`.
`StageEvent.downstream` is a single field: when both services fail it names air-rag first and
the detail names both. That limit is the event model's, recorded in §8.

**Proposing.** When the plan carries a write step:
- the principal's `allow_actions` must be true (`REQ-TGW-020`), else the turn routes `direct`
  with reason "this key may not propose changes";
- the step is checked like a read, against `write` entries;
- then `ToolsGatewayClient.propose()` is called.

A good response becomes a `PendingProposal` carrying the gateway's id, the creating `key_id`
and `subject`, the capability, the arguments, risk and summary. `expires_at` is
`min(gateway, session TTL)` (`REQ-TGW-013`, `014`, `019`). The published `Proposal` uses this
service's `prop_` id as today. A `422` problem maps its `code` to a fixed answer, and a
failure (timeout or 5xx) to the "could not prepare that change now" answer, per spec §7.

**Executing.** `_resolve_confirmation()` still consumes the pending proposal first
(`REQ-MUT-006`, `010`). It now also compares `key_id` and `subject`, so a mismatch is consumed
and executes nothing, recorded as `rejected` (`REQ-TGW-019`). A confirmed proposal with a
gateway id is executed inside `GATHER`'s slot: `ActionExecutor.execute()` with
`Idempotency-Key: airo-<prop_id>` (`REQ-TGW-015`). There are no retries anywhere: the client
base is single-attempt, and the proposal is already consumed (`REQ-TGW-018`). The outcome
picks one of five fixed answer texts:
- `done`;
- `pending_approval`, which says it can be checked later (§9 Q6);
- `rejected` with a code;
- unknown, for a timeout, unreachable gateway or error;
- not done, for an open breaker.

It also writes one `action.executed` audit line (`REQ-TGW-017`, `021`). A pending proposal
without a gateway id, made by the `/propose` stand-in, keeps today's canned path unchanged.

**Answers on proposal and confirmation turns stay canned**, as today. The proposal turn's
text is built from the gateway's `summary`. Having the model word them is spec 0013's call.

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `engine/plan.py` | new | `Step`, `Plan`, `Planner` protocol, `RulePlanner` (today's rule, moved, behaviour identical) |
| `clients/tools_gateway.py` | new | `ToolsGatewayClient`, `ActionExecutor`, `Capability`, `Inventory`, `ToolResult`, `GatewayProposal`, `ExecuteOutcome`, `ToolsGatewayError` |
| `clients/base.py` | modified, additive | `get_json()` beside `post_json()`; `post_json(..., headers=)` for `Idempotency-Key`; both on one private `_request()` so the failure taxonomy (spec 0010) stays in one place |
| `engine/turn.py` | modified | takes `planner`, `tools`, `executor`; routing block replaced by `planner.plan()`; `_gather` fans out; `_lookups_message`; propose and execute branches; confirmation key/subject check |
| `config.py` | modified | `ToolsGatewaySettings`; `DownstreamSettings.tools_gateway` replaces `tools` and `action`; a tombstone for the retired sections (§7); `routes()` |
| `constants.py` | modified, **breaking** | `DownstreamService.TOOLS_GATEWAY = "air-tools-gateway"`; `TOOLS` and `ACTION` removed (`REQ-TGW-023`) |
| `schemas/common.py` | modified, additive | `Principal.subject: str \| None = None`, `Principal.allow_actions: bool = False` (fail closed for any principal built by hand) |
| `security/api_keys.py` | modified | `to_principal()` copies `allow_actions` (closes the `REQ-MUT-012` gap); `subject` stays `None` until spec 0014 |
| `schemas/session.py` | modified, additive | `PendingProposal`: `capability`, `gateway_proposal_id`, `key_id`, `subject`. `SessionView` unchanged, so none of them is shown |
| `schemas/chat.py`, `schemas/events.py` | modified, docs | `Proposal` docstring names the gateway, not air-action; `RouteEvent.capabilities` description becomes "capabilities this turn's plan used" (`REQ-TGW-025`) |
| `api/deps.py`, `main.py`, `api/v1/chat.py`, `api/v1/query.py` | modified | `AppState.tools_gateway`, `AppState.action_executor`; passed to the engine; lifespan closes both |
| `api/v1/system.py` | modified | `/v1/capabilities` filters `tools`/`action` by the caller's channel inventory (`REQ-TGW-005`); `/v1/ready`'s not-probed list loses `TOOLS`/`ACTION` and gains `TOOLS_GATEWAY` |
| `observability/metrics.py` | modified | `record_capability_call(capability, kind, outcome)` (`REQ-TGW-022`) |
| `prompts/registry.py` | unchanged | `customer_care` stays v2. The lookup rules travel in the engine's lookups message (`REQ-TGW-027`), so a turn without lookups keeps the reviewed prompt (tasks T7) |
| `pyproject.toml` | modified | runtime `jsonschema>=4.23` (Draft 2020-12 validation of capability arguments); dev `pyyaml`, `types-PyYAML` (the test double loads the contract) |
| `.env.example`, `docker-compose.yml` | modified | `DOWNSTREAM__TOOLS_GATEWAY__*`; air-tools-gateway on air-net as `http://air-tools-gateway:8080` |
| `docs/02-lld.md` §1, §8, §15; `docs/openapi.json` | modified | layout, the two-object split, status; regenerated |

**Not changed, though a reviewer might expect them to be:**

- **`clients/rag.py` and spec 0011's behaviour.** Retrieval runs concurrently with lookups
  now, but its call, selection, prompt block and citations are untouched. The concurrency
  lives in `_gather`, not in the client.
- **`schemas/events.py` event names and `Stage`.** No `execute` stage and no new event.
  Execution reports in `GATHER`'s slot. `proposal`, `route`, `stage`, `answer` and
  `turn.end` already say everything spec 0012 needs (§5).
- **`memory/session.py`.** `set_pending` / `take_pending` / `clear_pending` already hold one
  pending proposal per session, single-use (`REQ-MUT-006`, `011`). Only the model they store
  grows.
- **`guardrails/*`.** Tool results go through the existing `screen_input()` and `delimit()`.
  No new rule.
- **`api/errors.py`.** Nothing new reaches a client as a problem. Every gateway failure is a
  turn outcome (Article III).
- **The `/propose` stand-in.** It still proposes `echo.noop`, because `RulePlanner` emits no
  gateway step. It is the stand-in until spec 0013 removes it.

## 3. Contract impact

- [ ] No change to public contracts
- [ ] Additive only
- [x] **Breaking**, and intended (`REQ-TGW-023`, Article VI): `DownstreamService` loses
  `air-tools` and `air-action` and gains `air-tools-gateway`. Both removed values appear in
  `/v1/ready`'s dependency list today, as unconfigured entries, and in the OpenAPI enum. No
  deployment has ever configured either, so no dashboard can have data under them. Callers
  that hard-code the two names in a ready check (air-web-console's System tab is the one to
  check, read-only) must be told. **`make openapi`** regenerates the enum.
- Additive besides: `PendingProposal` fields (server-side only), `Principal` fields (internal),
  `degraded` naming `air-tools-gateway`, and `route` events listing capability names.

## 4. Configuration

`AIR_ORCHESTRATOR_SERVICE__DOWNSTREAM__TOOLS_GATEWAY__*`:

| Setting | Default | Why it is configurable |
| --- | --- | --- |
| `ENABLED` | `false` | Optional downstream; a fresh checkout must not report itself degraded |
| `BASE_URL` | `http://localhost:8084` | Host route; compose overrides it with `http://air-tools-gateway:8080` |
| `API_KEY` / `EXECUTE_KEY` | unset | Single-tenant pair, used only when `TENANT_KEYS` is empty (`REQ-TGW-007`) |
| `TENANT_KEYS` | `{}` | JSON, tenant → `{"read": "...", "execute": "..."}` (spec §9 Q2). A tenant missing from a non-empty map gets no capabilities |
| `TIMEOUT_MS` | `3000` | Per call; narrowed by the deadline as usual |
| `EXECUTE_TIMEOUT_MS` | `8000` | Execute talks to a system of record and is never retried, so it gets more room than a lookup |
| `DEADLINE_SHARE` | `0.3` | Lookups run alongside retrieval (0.2), not after it |
| `INVENTORY_TTL_S` | `60` | How long an added or removed capability takes to show (§9 Q3) |
| `INVENTORY_GRACE_S` | `300` | How long the last good inventory survives failed refreshes (§9 Q3). Must be ≥ `INVENTORY_TTL_S` |
| `MAX_RESULT_CHARS` | `4000` | Bound on one result in the prompt (`REQ-TGW-009`) |
| `ALLOW__CUSTOMER` / `ALLOW__BUSINESS` | `[]` / `[]` | Names or `namespace.*` patterns per channel (Article I, §9 Q4). Empty by default (`REQ-TGW-004`); SIT sets `["orders.*"]` on customer. On business, a pattern that matches a `write` still admits nothing (`REQ-TGW-026`) |

Validation: `TIMEOUT_MS`/`EXECUTE_TIMEOUT_MS` 100–30000, `DEADLINE_SHARE` (0, 1],
`INVENTORY_TTL_S` 5–3600, `INVENTORY_GRACE_S` ≥ TTL and ≤ 3600, `MAX_RESULT_CHARS` 200–20000. Each pattern must match the
capability-name grammar with an optional trailing `.*`; anything else fails startup.

## 5. Alternatives rejected

| Alternative | Why not |
| --- | --- |
| One client object with `run`, `propose` and `execute` | Puts the execute key within reach of the code the planner drives, and makes "the read path cannot execute" a convention again. Two objects cost one class |
| One breaker for both clients | Five failed lookups would refuse a customer's confirmed cancel, and a failing execute would hide every lookup |
| A new `Stage.EXECUTE` | `Stage` is a closed set that clients render in order. air-web-console draws stage rows from it, so a new member is visible churn for one event per confirmed turn. `GATHER`'s slot is where a confirmation turn otherwise does nothing |
| Letting the planner pass `subject` as an argument and checking it | Leaves a model-written value one bug from the request. The envelope field, from the principal only, removes the possibility (`REQ-TGW-008`) |
| Sending the whole inventory in the `route` event, as its description says today | Hands every customer-channel caller the reconnaissance `/v1/capabilities` gates behind `admin:read` (`REQ-TGW-025`) |
| Retrying execute once on a timeout, with the same idempotency key | The gateway would make it safe, but "never retried here" keeps one owner for the decision. A customer confirming again goes through a fresh proposal, which re-checks state |
| Validating arguments with pydantic models generated from the schemas | Turns a downstream's runtime document into Python types at runtime, for no gain over a JSON Schema validator. `jsonschema` is the reference implementation of the standard the contract names |
| Keeping `DownstreamService.TOOLS`/`ACTION` and mapping both to the gateway | Two labels for one service splits its metrics and keeps a design that no longer exists in every dashboard query |
| Refusing startup when the retired `DOWNSTREAM__TOOLS__*`/`ACTION__*` settings are present | Every `.env` made by `make env` sets them (`ENABLED=false`). Refusing only when one of them is `ENABLED=true` catches the deployment that meant something by it, as the `llm` tombstone does for its section |
| Having the model word the proposal and confirmation answers now | It needs the planner's prompt (spec 0013). Canned texts from the gateway's summary and closed codes cannot misstate an outcome, which matters most here |

## 6. Test strategy

Hermetic throughout. The gateway is a respx double at `http://air-tools-gateway.invalid:8084`
(new `TOOLS_GATEWAY_BASE_URL` in `tests/conftest.py`, beside the other `.invalid` hosts).
`/v1/capabilities` probing gets fixtures in both directions, `reachable_tools_gateway` and
`unreachable_tools_gateway`, like the infra and model-gateway pairs. The planner is
`FixedPlanner`. The model gateway keeps its scripted stream, which records the messages it
was sent, so prompt content is asserted directly. Expiry uses an injected clock.

| Requirement | Test | Fixture / approach |
| --- | --- | --- |
| `REQ-TGW-001` | `test_tools_inventory.py::test_a_new_capability_in_an_allowed_namespace_needs_no_change` | Two inventory responses either side of a TTL step (injected clock) |
| `REQ-TGW-002` | `…::test_a_malformed_entry_is_dropped_and_the_rest_kept` | Parametrised over each rule; `caplog` for the one line |
| `REQ-TGW-003` | `…::test_a_failed_fetch_leaves_no_capabilities_and_the_turn_answers` | respx 503 and `ConnectError`, cold cache; then a warm cache inside and past the grace window (injected clock) |
| `REQ-TGW-004` | `test_tools_stage.py::test_a_capability_outside_the_channel_allow_list_is_never_called` | Allow-list `orders.get` only; plan names `orders.list_recent`; respx route `call_count == 0` |
| `REQ-TGW-005` | `test_system_routes.py::test_capabilities_list_tools_and_action_only_when_the_gateway_offers_them` | Inventories with read only, write only, none; unreachable fixture |
| `REQ-TGW-006` | `test_tools_stage.py::test_an_unknown_name_or_invalid_arguments_are_never_sent` | AC-2 |
| `REQ-TGW-007` | `test_tools_gateway_client.py::test_each_tenant_is_called_with_its_own_key` | Two tenants in `TENANT_KEYS`; header assertions; unmapped tenant gets no request |
| `REQ-TGW-008` | `test_tools_stage.py::test_the_subject_comes_only_from_the_principal` | AC-3; plus `requires_subject` entry hidden for a subject-less principal |
| `REQ-TGW-009` | `…::test_a_result_is_screened_delimited_and_cut` | An injection string inside an order note; an oversize result |
| `REQ-TGW-010` | `…::test_reads_run_concurrently_and_a_failure_degrades_gather` | Two reads each sleeping 0.2 s finish in under 0.35 s; one times out, giving `degraded` naming `air-tools-gateway` |
| `REQ-TGW-011` | `…::test_a_failed_read_puts_a_notice_in_the_prompt` | Recorded model messages |
| `REQ-TGW-012` | `test_action_execution.py::test_a_proposing_turn_sends_no_execute_request` | AC-4; respx asserts no `/execute` route was hit |
| `REQ-TGW-013` | `…::test_the_gateway_proposal_id_never_reaches_the_caller` | Search every event's JSON and `GET /v1/sessions/{id}` for the `gp_` id |
| `REQ-TGW-014` | `…::test_the_proposal_takes_the_earlier_expiry_and_a_known_risk` | Gateway expiry before and after the TTL; `risk: "extreme"` gives no proposal |
| `REQ-TGW-015` | `…::test_the_idempotency_key_is_derived_from_the_proposal_id` | Header equals `airo-<prop_id>` |
| `REQ-TGW-016` | `test_tools_gateway_client.py::test_only_the_execute_request_carries_the_execute_key` | Every respx call's `X-API-Key`; `caplog` and `repr()` free of both keys |
| `REQ-TGW-017` | `test_action_execution.py::test_the_answer_follows_the_execute_outcome` | Parametrised: `done`, `pending_approval`, `rejected/invalid_state`, 504, 503, `ConnectError`, breaker open; none of the non-done texts contains "cancelled" |
| `REQ-TGW-018` | `…::test_an_execute_failure_is_never_retried` | 504, then the same confirmation again: one execute call in total |
| `REQ-TGW-019` | `…::test_a_confirmation_from_another_key_or_subject_executes_nothing` | AC-7, answer compared with confirming an unknown id |
| `REQ-TGW-020` | `…::test_a_key_without_allow_actions_sends_no_propose_request` | Key record `allow_actions: false` |
| `REQ-TGW-021` | `…::test_an_execution_writes_one_audit_line_without_arguments` | `caplog`; argument values absent |
| `REQ-TGW-022` | `test_tools_gateway_client.py::test_every_capability_call_is_counted` | Registry sample before and after |
| `REQ-TGW-023` | `test_system_routes.py::test_the_gateway_is_labelled_air_tools_gateway` | `/v1/ready` lists it; neither old name is present |
| `REQ-TGW-024` | `tests/integration/test_tools_gateway_contract.py::test_the_double_refuses_what_the_contract_refuses` | The double loads `contract.openapi.yaml`, validates every request body against it, and returns 403 on a wrong-scope key; a real-app turn through it passes, and a hand-made bad request fails |
| `REQ-TGW-025` | `test_tools_stage.py::test_the_route_event_names_only_the_capabilities_used` | Inventory of three, plan using one |
| `REQ-TGW-026` | `test_action_execution.py::test_the_business_channel_is_never_offered_a_write` | Business allow-list `["orders.*"]`; a planned `orders.cancel` sends no propose request |

**Existing tests that change,** to be done in the same task as the code that moves them:
- `test_config.py::test_every_optional_service_defaults_to_disabled` and
  `test_system_routes.py::test_ready_ignores_optional_downstreams` (both iterate the service
  list);
- `test_channels.py::test_anonymous_identity_cannot_propose_mutations`, which now proves behaviour, not a field,
  because `REQ-MUT-012` becomes enforced;
- spec 0001's traceability row for `REQ-MUT-012`, moved from `GAP` to that test.

## 7. Rollout

- Off by default (`ENABLED=false`), and unreachable from a real turn until spec 0013's
  planner emits gateway steps. Merging this changes nothing for a deployment except the
  `/v1/ready` label change (§3) and `allow_actions` now being enforced.
- **`allow_actions` enforcement is a behaviour change on its own.** Keys whose record sets it
  `false`, the anonymous development identity among them, stop getting `/propose` proposals.
  The SIT keys from `make env` are unaffected because they leave it at its default `true`.
  Called out in the PR.
- Tombstone: `DOWNSTREAM__TOOLS__*` and `DOWNSTREAM__ACTION__*` are accepted and ignored with
  one startup warning. Startup fails only if either sets `ENABLED=true`, with a message naming
  `DOWNSTREAM__TOOLS_GATEWAY__*`. `.env.example` drops them.
- SIT, after air-tools-gateway's `make up`: set `ENABLED=true`, the brightcart key pair, and
  `ALLOW__CUSTOMER=["orders.*"]`. A principal with a subject has to be hand-built until spec
  0014, so the joint SIT for per-customer capabilities waits for 0014. Until then it can cover
  the inventory, the allow-list, degradation and the contract.
- Turning it off: `ENABLED=false`. No data is migrated.

## 8. Risks

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| A tool result carries an injection (an order note written by an attacker) | Medium | Screened and delimited like a passage (`REQ-TGW-009`); worst case is a proposal the customer never confirms, because execute needs the structured field |
| The two stage-event fields cannot name two failed services | Certain, when both fail | Detail names both; `turn.end.degraded` comes from stage events, so it names one. Recorded here; a list-valued field would be an additive change if it starts to matter |
| A slow gateway inventory fetch adds latency to every turn | Medium | Cached per tenant; fetched at `PLAN` within the gateway's deadline share (so the planner chooses from it and the `route` event is checked against it, `REQ-TGW-025`), never before the first event, and not at all on a confirmation turn or for a channel with an empty allow-list; a miss on a cold cache is one call per tenant per TTL |
| `jsonschema` adds a runtime dependency | Low | The reference implementation of the standard the contract names; pure Python; schemas are compiled once per inventory refresh |
| An execute times out and the customer is told "unknown" | Medium | Honest by design (`REQ-TGW-017`). The answer tells them how to check (`orders.get`). The gateway's idempotency makes a later re-proposal safe |
| A breaking label change surprises a consumer | Low | Neither label ever carried data; flagged in §3 and the PR |
