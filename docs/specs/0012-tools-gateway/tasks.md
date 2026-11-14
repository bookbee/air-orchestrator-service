# 0012 — Tasks

**Spec:** [`spec.md`](spec.md) · **Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-14

> Each task leaves the tree green (`make check`). Each names the requirement it advances and
> the test that proves it, exactly as spec §10 names them. When a task lands, its rows in
> spec §10 move from `DEFERRED` to the test.

## Order

```
T1 ─┬─► T3 ─► T4 ─► T5 ─► T6 ─► T7 ─► T8 ─► T9 ─► T10 ─► T11 ─► T12
    └─► T2 ──────────────────────────► (before T8)
```

- **T1 first.** It replaces the `DownstreamService` members and settings that every later
  task names.
- **T2 before T8.** `allow_actions` and `subject` must be on `Principal` before a real
  proposal can be made (`REQ-TGW-020`, `REQ-MUT-012`). It is otherwise independent and can
  land any time after T1.
- **T3 → T4.** The inventory needs `get_json()`, and execute needs `post_json(headers=)`.
- **T5 before T6.** `GATHER` reads its steps from a `Plan`. T5 moves today's routing behind
  the seam with no behaviour change, so its review is "nothing moved".
- **T7 after T6.** It reuses T6's step validation, for `write` entries.
- **T8 after T7.** Executes what T7 stores.
- **T9 after T8.** It runs every gateway request shape through the contract double, so it
  needs them all.
- **T10 needs a running air-tools-gateway.** The rest is hermetic.

## Tasks

### T1 — The `air-tools-gateway` label and its settings

- **Advances:** `REQ-TGW-023`
- **Touches:**
  - `constants.py`: `TOOLS_GATEWAY` added; `TOOLS` and `ACTION` removed;
  - `config.py`: `ToolsGatewaySettings` with every plan §4 setting and its bounds;
    `DownstreamSettings.tools_gateway` replacing `tools` and `action`; `_SERVICE_ATTR`;
    `routes()`; the tombstone for `DOWNSTREAM__TOOLS__*` / `ACTION__*`;
  - `api/v1/system.py`: the `/v1/ready` not-probed list;
  - `.env.example`;
  - `docs/openapi.json` (`make openapi`).
- **Adds test:**
  - `tests/unit/test_system_routes.py::test_the_gateway_is_labelled_air_tools_gateway`;
  - in `test_config.py`, the tombstone: ignored with a warning when `ENABLED=false`, startup
    refused when `ENABLED=true`;
  - updates `test_config.py::test_every_optional_service_defaults_to_disabled` and
    `test_system_routes.py::test_ready_ignores_optional_downstreams`.
- **Done when:**
  - `/v1/ready` lists `air-tools-gateway`, unconfigured, and neither `air-tools` nor
    `air-action`;
  - a `.env` made by the old `make env` still boots, with one warning;
  - `docs/openapi.json` shows the new enum.

### T2 — `Principal` carries `allow_actions` and `subject`; `REQ-MUT-012` enforced

- **Advances:** `REQ-MUT-012` (spec 0001), and groundwork for `REQ-TGW-008`, `020`
- **Touches:**
  - `schemas/common.py`: `Principal.allow_actions: bool = False`,
    `Principal.subject: str | None = None`;
  - `security/api_keys.py`: `to_principal()` copies `allow_actions`;
  - `engine/turn.py`: the `/propose` branch routes `direct` when `allow_actions` is false.
- **Adds test:** `tests/unit/test_channels.py::test_anonymous_identity_cannot_propose_mutations`,
  rewritten to drive a turn with `/propose` as the anonymous identity and assert no `proposal`
  event. Spec 0001 §10's `REQ-MUT-012` row moves from `GAP` to it, and the "What this table
  found" note records the closure.
- **Done when:** a key record with `allow_actions: false` gets a direct answer to `/propose`
  and no proposal, while a key that grants it (the test keys, which take the record's
  `true` default) still gets one. `make env`'s development keys set `allow_actions: false`,
  so they stop getting proposals; exercising the propose path by hand needs a key that
  grants it.

### T3 — `get_json()` and per-call headers on the client base

- **Advances:** groundwork for `REQ-TGW-001`, `015` (spec 0010's taxonomy, extended)
- **Touches:** `clients/base.py`: a private `_request()` under both `post_json()` and a new
  `get_json()`, and `post_json(..., headers=)`. The module docstring explains why both go
  through one path.
- **Adds test:** in `tests/unit/test_client_base.py`:
  - `test_get_json_fails_with_the_same_four_reasons`, parametrised as the POST case;
  - `test_extra_headers_are_sent_but_never_replace_the_key`.
- **Done when:** both new tests pass, and every existing spec 0010 test passes unchanged.

### T4 — `ToolsGatewayClient`: inventory, tenant keys, metric

- **Advances:** `REQ-TGW-001`, `002`, `003`, `007`, `022`
- **Touches:**
  - `clients/tools_gateway.py` (new): `ToolsGatewayClient.inventory()` / `run()` /
    `propose()`, `Capability`, `Inventory`, per-tenant cache with TTL and grace, `jsonschema`
    validator per entry;
  - `observability/metrics.py`: `record_capability_call`;
  - `pyproject.toml`: `jsonschema`;
  - `api/deps.py`, `main.py`: `AppState.tools_gateway`, built when configured, closed in the
    lifespan;
  - `tests/conftest.py`: `TOOLS_GATEWAY_BASE_URL` at an `.invalid` host.
- **Adds test:**
  - in `tests/unit/test_tools_inventory.py`:
    - `test_a_new_capability_in_an_allowed_namespace_needs_no_change`;
    - `test_a_malformed_entry_is_dropped_and_the_rest_kept`;
    - `test_a_failed_fetch_leaves_no_capabilities_and_the_turn_answers`: the client half;
      the turn half lands in T6;
  - in `tests/unit/test_tools_gateway_client.py`:
    - `test_each_tenant_is_called_with_its_own_key`;
    - `test_every_capability_call_is_counted`.
- **Done when:**
  - two tenants get two inventories, each fetched with its own read key;
  - a malformed entry is logged once and dropped;
  - a 503 inside the grace window serves the last list, and past it serves none (injected
    clock).

### T5 — The planner seam, behaviour unchanged

- **Advances:** none directly. It is what lets T6–T8 be driven by an injected plan (spec §3).
- **Touches:**
  - `engine/plan.py` (new): `Step`, `Plan`, `Planner`, `RulePlanner`;
  - `engine/turn.py`: the routing block becomes `await self._planner.plan(...)`;
    `TurnEngine(planner=...)` defaults to `RulePlanner`;
  - `tests/conftest.py`: a `FixedPlanner` helper.
- **Adds test:** `tests/unit/test_plan.py::test_the_rule_planner_reproduces_todays_routes`,
  table-driven over customer/business, rag configured or not, `/propose`, and a confirmation.
- **Done when:**
  - the whole existing suite passes unchanged: same route events, same reasons;
  - `RulePlanner` emits no gateway step on any input.

### T6 — Lookups in `GATHER`

- **Advances:** `REQ-TGW-004`, `006`, `008`, `009`, `010`, `011`, `025`, and the turn half
  of `003`
- **Touches:**
  - `engine/turn.py`:
    - the turn's inventory filtered by channel allow-list, business writes removed,
      `requires_subject`;
    - step validation;
    - retrieval and reads run concurrently;
    - `_lookups_message()`, notices;
    - the stage detail;
    - `RouteEvent.capabilities` carries the capabilities used;
  - `schemas/events.py`: `RouteEvent.capabilities` description.
- **Adds test:** in `tests/unit/test_tools_stage.py`:
  - `test_a_capability_outside_the_channel_allow_list_is_never_called`;
  - `test_an_unknown_name_or_invalid_arguments_are_never_sent`;
  - `test_the_subject_comes_only_from_the_principal`;
  - `test_a_result_is_screened_delimited_and_cut`;
  - `test_reads_run_concurrently_and_a_failure_degrades_gather`;
  - `test_a_failed_read_puts_a_notice_in_the_prompt`;
  - `test_the_route_event_names_only_the_capabilities_used`;
  - and the turn half of
    `tests/unit/test_tools_inventory.py::test_a_failed_fetch_leaves_no_capabilities_and_the_turn_answers`.
- **Done when:** an injected plan with one good and one bad step:
  - sends exactly one request, with the principal's subject;
  - puts its result in the prompt as delimited data, and the bad step as a notice;
  - the gateway down instead reports `GATHER` as `degraded` naming `air-tools-gateway`, and
    the turn answers.

### T7 — The rules for lookups travel with them; `customer_care` stays v2

- **Advances:** `REQ-TGW-011` (the answer acts on the notice), `027`, supporting `009`
- **Was:** a prompt `customer_care:v3` carrying the lookup rules. Dropped (2026-11-15) for
  the design in `REQ-TGW-027`: the rules travel with the lookups, so a turn without them keeps
  the reviewed v2 unchanged. The live review of three v3 wordings also found the model
  promising lookups it could not make ("Can you give me your order number so I can look into
  what's going on?"). That first looked like v3's doing, but a final run of v2 under the new
  `promises_check` flag did the same in 2 of 7 conversations. **It is a v2 defect**, which
  the earlier v2 runs missed on small samples whose first turn was `degraded` by downstream
  timeouts. Fixing it is a prompt change of its own (a new version, reviewed over several
  samples with stable downstreams), not part of this spec.
- **Touches:**
  - `engine/turn.py`: `_LOOKUPS_HEADER` and `_LOOKUPS_FAILED` carry the rules (exact facts,
    nothing missing, partial, no mention of the mechanism, could-not-check, next step);
  - `tools/prompt_review.py`: the `promises_check` flag; `"requires": "lookups"`
    conversations, skipped unless `--lookups` (`make prompt-review LOOKUPS=1`), where only a
    claimed *change* counts as `claims_action`;
  - `evals/prompts/customer_care.json`: `order-status-with-lookup` and
    `order-status-lookup-fails`, both `requires: lookups`.
- **Adds test:**
  - `tests/unit/test_tools_stage.py::test_the_rules_for_lookups_travel_only_with_lookups`;
  - `tests/unit/test_prompts.py::test_the_lookups_message_carries_the_rules_for_using_them`;
  - in `tests/unit/test_prompt_review.py`: `promises_check` cases,
    `test_a_lookups_conversation_flags_only_claimed_changes`,
    `test_a_lookups_conversation_is_skipped_unless_asked_for`.
- **Done when:**
  - the unit tests pass;
  - a turn without lookups carries no lookups message, so its prompt is v2's;
  - `make prompt-review` on the no-lookups set runs the same prompt as before (v2); its
    `promises_check` flags are v2's own, tracked as the follow-up above;
  - the two `requires: lookups` conversations are reviewed where a real turn can first look
    something up: spec 0013's T7, once spec 0014's T2 has landed (the gateway, a planner that
    plans lookups, and a verified customer).

### T8 — Proposing a `write` capability

- **Advances:** `REQ-TGW-012`, `013`, `014`, `020`, `026`
- **Touches:**
  - `schemas/session.py`: `PendingProposal` gains `capability`, `gateway_proposal_id`,
    `key_id`, `subject`;
  - `engine/turn.py`: the propose branch for a gateway step (allow-actions check, write
    validation, `propose()`, expiry and risk, canned answers from `summary` and closed
    codes);
  - `schemas/chat.py`: the `Proposal` docstring.
- **Adds test:** in `tests/unit/test_action_execution.py`:
  - `test_a_proposing_turn_sends_no_execute_request`;
  - `test_the_gateway_proposal_id_never_reaches_the_caller`;
  - `test_the_proposal_takes_the_earlier_expiry_and_a_known_risk`;
  - `test_a_key_without_allow_actions_sends_no_propose_request`;
  - `test_the_business_channel_is_never_offered_a_write`.
- **Done when:**
  - an injected `orders.cancel` plan produces one propose request, a `prop_` proposal, and
    no `/execute` call;
  - a `gp_` id appears in no event and no session view.

### T9 — Executing a confirmed proposal

- **Advances:** `REQ-TGW-015`, `016`, `017`, `018`, `019`, `021`
- **Touches:**
  - `clients/tools_gateway.py`: `ActionExecutor`, with its own breaker and the execute key;
  - `api/deps.py`, `main.py`: `AppState.action_executor`, kept out of `optional_clients()`;
  - `engine/turn.py`:
    - `_resolve_confirmation()` compares `key_id` and `subject`;
    - execute in `GATHER`'s slot, with the five outcome texts and the audit line;
  - `observability/logging` use only.
- **Adds test:**
  - in `tests/unit/test_action_execution.py`:
    - `test_the_idempotency_key_is_derived_from_the_proposal_id`;
    - `test_the_answer_follows_the_execute_outcome`;
    - `test_an_execute_failure_is_never_retried`;
    - `test_a_confirmation_from_another_key_or_subject_executes_nothing`;
    - `test_an_execution_writes_one_audit_line_without_arguments`;
  - `tests/unit/test_tools_gateway_client.py::test_only_the_execute_request_carries_the_execute_key`.
- **Done when:**
  - a confirmed `orders.cancel` sends one execute request, with the execute key and
    `Idempotency-Key: airo-<prop_id>`;
  - every outcome gives its text, and no text but `done`'s says it was done;
  - an open breaker on lookups does not refuse an execute.

### T10 — The contract double

- **Advances:** `REQ-TGW-024`
- **Touches:**
  - `tests/integration/tools_gateway_double.py` (new): a respx double that loads
    `docs/specs/0012-tools-gateway/contract.openapi.yaml`, validates every request body
    against it, and enforces key scopes;
  - `pyproject.toml`: dev `pyyaml`, `types-PyYAML`.
- **Adds test:** `tests/integration/test_tools_gateway_contract.py::test_the_double_refuses_what_the_contract_refuses`,
  plus one real-app turn per flow (lookup, propose, execute) through the double.
- **Done when:**
  - all three flows pass through the double;
  - a hand-made request with an extra field, or a read key on execute, fails.

### T11 — SIT against the real gateway

- **Advances:** none new. It proves T4–T9 against air-tools-gateway's own build.
- **Touches:**
  - `docker-compose.yml`: `DOWNSTREAM__TOOLS_GATEWAY__BASE_URL: http://air-tools-gateway:8080`;
  - `.env.example`: commented SIT values: brightcart key pair, `ALLOW__CUSTOMER=["orders.*"]`.

  No debug-only path into the engine is added to drive a plan by hand. Until spec 0013's
  planner exists, SIT covers what a real turn can reach: the inventory in
  `/v1/capabilities`, and degradation with the gateway stopped. The contract double's three
  flows are also replayed against the live gateway with its development keys, so the two
  implementations of the contract are checked against each other.
- **Done when:**
  - with air-tools-gateway up, `/v1/capabilities` (admin key) lists `tools` and `action`,
    and the downstream `air-tools-gateway` as reachable;
  - with it stopped, both routes disappear and the probe says why.

### T12 — Close-out

- **Advances:** all, by closing the loop
- **Touches:**
  - `docs/02-lld.md`: §1 layout (`tools_gateway.py`, `plan.py` built), §8 the two-object
    split as built, §15 Phase 3/4 status;
  - `docs/openapi.json` (`make openapi`);
  - spec 0012 §10: every row a test; status `Implemented`, or `Partial` with the gaps named;
  - CLAUDE.md "Real today" and "Where to start".
- **Adds test:** —
- **Done when:** `make check` and `make trace` pass, with no `DEFERRED` row left in spec 0012
  except any recorded as a `GAP` with its reason.

## Definition of done

- [ ] Every requirement in `spec.md` §5 traces to a passing test, or is a declared `GAP`
- [ ] `make check` passes (ruff · mypy --strict · pytest)
- [ ] `make trace` passes
- [ ] `make openapi` run and `docs/openapi.json` updated (T1, T12)
- [ ] `make prompt-review` on the no-lookups set runs v2 unchanged (T7); the `LOOKUPS=1` run is spec 0013 T7's
- [ ] Spec status moved to `Implemented` (or `Partial`, with gaps listed)
- [ ] Module docstrings explain the *why*: `clients/tools_gateway.py` (two objects, two keys,
      two breakers), `engine/plan.py` (the seam, and why `RulePlanner` never emits a gateway step)
- [ ] `docs/02-lld.md` §15 build status updated
