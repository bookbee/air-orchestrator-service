# 0013 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Built · **Updated:** 2026-11-15

> Builds on spec 0012's planner seam (its T5: `engine/plan.py`, `Planner`, `Plan`, `Step`,
> `RulePlanner`) and its turn inventory (its T6). Nothing here starts before those land.

## 1. Approach

**`ModelPlanner` behind the existing seam.** Spec 0012 puts routing behind
`Planner.plan(ctx) -> Plan`. This spec adds a second implementation and a second method:

```python
class Planner(Protocol):
    async def plan(self, ctx: PlanContext) -> PlanOutcome: ...
    async def follow_up(self, ctx: PlanContext, first: Plan, results: list[ReadResult]) -> FollowUp: ...
```

- `PlanContext`: the masked text, the session's last `history_turns` turns, the classifier
  verdict, the turn's offered capabilities (spec 0012's filtered inventory), the turn
  deadline, and a cost allowance.
- `PlanOutcome` = the `Plan`, plus a stage verdict (`ok` / `skipped` / `degraded` with
  detail), plus cost.
- `RulePlanner.follow_up` always returns "no change".

`create_app` picks `ModelPlanner` when `planner.enabled`, else `RulePlanner`.
`ModelPlanner` holds a `RulePlanner(stand_in=False)` as its fallback. So once the planner is
enabled, `/propose` is inert even when planning fails (`REQ-PLN-010`).

**Round 1.** `ModelPlanner.plan()` checks the conditions in `REQ-PLN-001`. If one fails it
returns the fallback plan, `skipped` with the reason, and makes no call. Otherwise it:
1. **Builds a per-turn JSON schema.** `capability` is an `enum` of the offered `read` names
   in `reads[]`, and of the offered `write` names in `propose`. `propose` is `null`-only
   when no write is offered: none on business, none without `allow_actions`. The model's
   choices are constrained before validation, not only after. `arguments` is a free object
   here; spec 0012 validates it against the capability's own schema afterwards
   (`REQ-PLN-006`).
2. **Renders `planner:v1`** from the registry. A system message carries the rules. A second
   system message holds `delimit(catalogue_json, source="capabilities")`: names, kinds,
   descriptions, input schemas, sorted by name. A third carries the masked recent turns and
   the classifier verdict. Last comes `delimit(masked_text, source="customer")`. Subject,
   tenant, keys and token never enter the builder's inputs at all: it takes a `PlanContext`
   that has no field for them (`REQ-PLN-002`).
3. **Calls the model.** First it estimates cost with the spec 0008 estimator, which moves
   out of `TurnEngine._estimate` into `engine/cost.py` as a pure function of settings, so the
   engine and the planner price calls identically. If the estimate does not fit what the turn ceiling leaves, it falls back,
   counted `skipped_budget`. Otherwise it calls `ModelGatewayClient.generate(messages,
   json_schema=…, schema_name="plan", model_class=planner.model_class,
   max_tokens=planner.max_tokens, timeout=bound)`. The bound is `min(deadline_share ×
   deadline, remaining)` (`REQ-PLN-005`, `014`).
4. **Parses and validates** with a pydantic `PlanOutput` model (`extra="forbid"`,
   `reads` ≤ `max_reads`, `reason` ≤ 200). Invalid JSON, a schema miss, `refusal`,
   `finish_reason="length"`, a `ModelGatewayError` or a timeout all become the fallback plan,
   with `degraded` naming `air-model-gateway` (`REQ-PLN-003`, `004`).
5. **Maps `PlanOutput` to spec 0012's `Plan`.** Routes follow from content: `tools` when
   there are reads, `action` when there is a propose, plus `rag` by rule on the customer
   channel (§9 Q3). The engine then runs spec 0012's step checks unchanged (`REQ-PLN-006`).

**Round 2.** `_gather` (spec 0012) runs the reads. When the plan set `needs_results` and at
least one read ran, the engine calls `planner.follow_up(ctx, plan, results)`. The results go
in exactly as the answer prompt will see them: screened, cut and delimited, reusing
`_lookups_message()`'s builder. The schema for this call allows only `{"propose": … | null,
"reason"}`, with no `reads` field at all, which is the "no further reads" rule in the schema
itself (`REQ-PLN-007`). The returned propose step goes through the same checks as a round-1
one (`REQ-PLN-008`). Its outcome is appended to `GATHER`'s detail, never emitted as a second
`PLAN` event (`REQ-PLN-011`). The engine calls `follow_up` at most once per turn, which is
the "never a third round" rule.

**What the client sees** is unchanged from spec 0012: `route` with the capabilities used,
stage details, `proposal`, `answer`. `PlanOutput.reason` goes to the log only
(`REQ-PLN-013`).

**As built, four details differ from the sketch above.**
- `PlanContext` keeps `principal`, because the rule routes on channel and
  `allow_actions`. The prompt builder, `planning_messages()`, takes the text, history,
  classifier labels and menu as separate arguments and never the context, so it still has
  no way to render an identity (`REQ-PLN-002`).
- `follow_up(ctx, first, results)` takes `results` as the delimited blocks the answer
  prompt carries (`TurnEngine._lookup_blocks()`), not a list of results. That makes "as
  the answer prompt will see them" literal.
- Round 2 is not called when no read returned anything: `GATHER` says `round 2: skipped:
  no lookup results`. Its failure leaves the reads in the answer prompt with a notice that
  the change could not be prepared (spec §7).
- The model planner also drops writes from its menu for a key without `allow_actions` and
  on the business channel, so `propose` is `null`-only for them, as step 1 says. The engine
  still drops such a write afterwards (`REQ-TGW-020`, `026`).

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `engine/plan.py` | modified | `PlanContext`, `PlanOutcome`, `FollowUp`; `Planner.follow_up`; `RulePlanner(stand_in=…)`; new `ModelPlanner` |
| `engine/planner_schema.py` | new | Per-turn JSON schemas for both rounds; `PlanOutput`, `FollowUpOutput` |
| `engine/cost.py` | new | `estimate_cost(settings, messages, max_tokens)`, moved verbatim from `TurnEngine._estimate`; the engine calls it, behaviour unchanged |
| `engine/turn.py` | modified | builds `PlanContext`; uses `PlanOutcome` for the `PLAN` event; calls `follow_up` after reads; appends its detail to `GATHER`; adds planning cost to usage and the running ceiling |
| `clients/model_gateway.py` | modified, additive | `generate(..., model_class: ModelClass \| None = None)` overrides the configured class for one call |
| `prompts/registry.py` | modified | `PLANNER = "planner"`, `planner:v1`, in `_LATEST`; pinnable via `prompts.pins` like the channel prompts |
| `config.py` | modified | `PlannerSettings` (§4) on `Settings.planner` |
| `schemas/common.py` | modified, additive | `Usage.planning_cost_usd` |
| `observability/metrics.py` | modified | `record_planner_call(round, outcome)` |
| `main.py`, `api/deps.py` | modified | chooses the planner; `AppState.planner` |
| `evals/planner/cases.json` | new | message, history, offered capabilities → expected plan shape |
| `tools/planner_eval.py`, `Makefile` | new, modified | `make planner-eval`: live, paid, outside `make check` |
| `docs/openapi.json` | regenerated | `Usage.planning_cost_usd` |

**Not changed, though a reviewer might expect them to be:**

- **`clients/tools_gateway.py` and every spec 0012 check.** The planner produces a `Plan`
  and nothing more. It never calls the gateway, and it gets no exemption from the
  allow-list, schema, subject or business-write rules. If a model plan could skip a check
  that a fixed plan cannot, the seam would be lying.
- **`schemas/events.py` and `constants.py`.** No new event, stage or route. The second round
  reports inside `GATHER` precisely so `Stage` order stays the contract it is.
- **`guardrails/*`.** The planner's input is already masked, and capability descriptions and
  read results go through the existing `delimit()` and, for results, `screen_input()`.
- **Spec 0011's retrieval.** It is still decided by rule and runs concurrently with the
  reads, as spec 0012 made it.
- **The synthesis prompt.** The planner's output never reaches it. The answer model sees
  lookup results and sources, not the plan.

## 3. Contract impact

- [ ] No change to public contracts
- [x] Additive only: `usage.planning_cost_usd`; `PLAN` stage details and `degraded` naming
  `air-model-gateway` on the `PLAN` stage. Both are already in the vocabulary. `make openapi`.
- [ ] Breaking

## 4. Configuration

`AIR_ORCHESTRATOR_SERVICE__PLANNER__*`:

| Setting | Default | Why it is configurable |
| --- | --- | --- |
| `ENABLED` | `false` | A fresh checkout makes no planning calls; SIT turns it on |
| `MODEL_CLASS` | `cloud` | §9 Q1: lowered when `make planner-eval` shows a cheaper class plans as well |
| `DEADLINE_SHARE` | `0.15` | Planning precedes everything slow; it must leave room for lookups and synthesis |
| `MAX_TOKENS` | `400` | A plan is short; a cap keeps a confused model cheap |
| `MAX_READS` | `3` | §9 Q4; schema-enforced |
| `HISTORY_TURNS` | `6` | Enough to resolve "the first one" without paying for the whole conversation |

Bounds: `DEADLINE_SHARE` (0, 0.5], `MAX_TOKENS` 100–2000, `MAX_READS` 1–5, `HISTORY_TURNS`
0–20. The two-round limit is a constant, not a setting (spec §9 Q4).

## 5. Alternatives rejected

| Alternative | Why not |
| --- | --- |
| A static plan schema, validated afterwards | The model could name anything and lose the turn's lookups to a typo. A per-turn `enum` puts the offered names in the grammar, and spec 0012's checks still run behind it |
| Emitting round 2 as a second `PLAN` stage event | Clients render stages in `Stage` order. A `PLAN` after `GATHER` is a contract change for one detail |
| One planning call that also writes the answer | Mixes a decision we validate with prose we guardrail, and makes the answer depend on the plan's model class. Two calls cost tokens and buy separable failure |
| Letting round 2 also add reads | Unbounded loops are how agents run up cost. "Compare and then cancel" is the deepest real case, and fits two rounds |
| Falling back to `RulePlanner` with the `/propose` stand-in | A planning failure would turn `/propose` back on for the very deployments that turned it off (`REQ-PLN-010`) |
| Classifier intent short-circuiting planning | Decided against for v1 (§9 Q2): measure first |
| Running the eval in `make check` with recorded responses | Recorded responses test the parser, which unit tests already do. The point of the eval is whether *a model* plans well, and that needs a live model |
| Putting the subject in the prompt "so the model knows who it is talking to" | The model has no use for it. Every capability that needs it gets it from the envelope (`REQ-TGW-008`) |

## 6. Test strategy

Hermetic. `model_gateway.generate` is reassigned per test to return scripted `GenerateResult`
JSON, to raise `ModelGatewayError`, or to sleep past the bound. It records each call's
`messages`, `json_schema` and `model_class`. The gateway is spec 0012's respx double. The
inventory is set per test.

| Requirement | Test | Approach |
| --- | --- | --- |
| `REQ-PLN-001` | `test_planner.py::test_a_turn_with_capabilities_is_planned_by_one_structured_call` | One call with `json_schema`; none with no capabilities, planner off, or a confirmation turn |
| `REQ-PLN-002` | `…::test_the_planner_prompt_holds_no_identity_and_only_offered_capabilities` | AC-5; search the recorded messages |
| `REQ-PLN-003` | `…::test_the_plan_is_validated_against_its_schema` | Parametrised bad outputs: four reads, extra key, wrong types |
| `REQ-PLN-004` | `…::test_an_invalid_plan_falls_back_and_the_turn_answers` | AC-3: prose, refusal, `length`, gateway error |
| `REQ-PLN-005` | `…::test_planning_is_bounded_and_counted_against_the_ceiling` | Recorded `timeout`; a tiny ceiling gives `skipped_budget` and no call |
| `REQ-PLN-006` | `…::test_the_planner_cannot_widen_the_offered_capabilities` | AC-4; respx `call_count == 0` |
| `REQ-PLN-007` | `…::test_a_dependent_proposal_takes_exactly_two_rounds` | AC-2; the round-2 schema has no `reads` |
| `REQ-PLN-008` | `…::test_round_two_arguments_must_validate` | Round 2 proposes `{"order_id": "55120; DROP"}`: no propose request |
| `REQ-PLN-009` | `test_prompts.py::test_the_planner_prompt_is_versioned_and_logged` | Registry ref; `caplog` shows `planner:v1` |
| `REQ-PLN-010` | `test_planner.py::test_the_propose_trigger_is_inert_when_the_planner_is_enabled` | AC-6, also with a failing planning call |
| `REQ-PLN-011` | `…::test_the_plan_stage_reports_what_ran` | Every skip reason, `ok` detail, round-2 detail on `GATHER` |
| `REQ-PLN-012` | `…::test_every_planning_call_is_counted` | Registry samples per round and outcome |
| `REQ-PLN-013` | `…::test_the_plan_never_reaches_the_client` | `reason` text and catalogue absent from every event and from `TurnResult` |
| `REQ-PLN-014` | `…::test_the_planner_uses_its_own_model_class` | Planner `local`, synthesis `cloud`: recorded classes differ |

## 7. Rollout

- Off by default. Turning it on needs spec 0012 configured with a non-empty allow-list, or
  every turn is skipped as "no capabilities offered", which is the safe failure.
- **Enabling it retires `/propose` for that deployment.** The SIT docs and the console's
  "propose" preset need a note. Tests that use `/propose` keep using `RulePlanner`.
- Joint SIT needs all of these: air-tools-gateway running with fixtures, spec 0014 for a
  subject, and air-web-console minting tokens. Before that, `make planner-eval` exercises
  planning alone against air-model-gateway.
- Turn off with `PLANNER__ENABLED=false`. No state.

## 8. Risks

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| Every planned turn gains a model call: latency and cost | Certain | 15% deadline share, 400-token cap, skipped when nothing is offered, cost shown separately in usage; `MODEL_CLASS` lowered after eval |
| The model plans badly (wrong capability, missing lookup) | Medium | Per-turn enums; the eval set before and after every prompt change; failure costs lookups, not the answer |
| A capability description tries to steer the planner | Low (the gateway is ours) | Delimited as data; output constrained to offered names; spec 0012 re-checks; writes still need confirmation |
| A lookup result injects into round 2 | Medium | Screened and delimited as in the answer prompt; round 2 can only propose an offered write with schema-valid arguments, which the customer must still confirm |
| Planning cost pushes turns into the ceiling | Low | Estimated before the call; skipped, not overspent |
