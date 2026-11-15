# 0013 — Tasks

**Spec:** [`spec.md`](spec.md) · **Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-15

> Each task leaves the tree green (`make check`). When one lands, its rows in spec §10 move
> from `DEFERRED` to the named test.

## Order

```
spec 0012 T5 (seam) + T6 (inventory, lookups) ──► T1 ─► T2 ─► T3 ─► T4 ─► T5 ─► T6 ─► T8
spec 0012 T8 (propose) ─────────────────────────────────────► T4
                                       T2 ──► T7 (eval, live) ──► T8
```

- **T1–T3 need spec 0012's T5 and T6.** The seam and the turn's offered capabilities are
  what the planner consumes.
- **T4 needs spec 0012's T8.** Round 2 produces a propose step, which needs the propose
  branch to exist.
- **T7 can run once T2 and T3 exist.** It only needs air-model-gateway. It must have run
  before `planner.enabled` is turned on anywhere.

## Tasks

### T1 — Planner settings and a per-call model class

- **Advances:** `REQ-PLN-014`
- **Touches:** `config.py` (`PlannerSettings`); `clients/model_gateway.py`
  (`generate(..., model_class=)`); `engine/cost.py` (the spec 0008 estimator moved out of
  `TurnEngine`, unchanged; every `test_cost_ceiling.py` test passes as is).
- **Adds test:** `tests/unit/test_planner.py::test_the_planner_uses_its_own_model_class`
  (client half: the body carries the override); bounds in `test_config.py`.
- **Done when:** a call with `model_class=local` sends `local` while the configured class is
  `cloud`, and every existing gateway test passes unchanged.

### T2 — `planner:v1` and the per-turn schemas

- **Advances:** `REQ-PLN-003`, `009`
- **Touches:** `prompts/registry.py` (`PLANNER`, `planner:v1`, `_LATEST`, pins);
  `engine/planner_schema.py` (round-1 and round-2 schemas from an inventory;
  `PlanOutput`, `FollowUpOutput`).
- **Adds test:** `tests/unit/test_prompts.py::test_the_planner_prompt_is_versioned_and_logged`
  (registry half); `tests/unit/test_planner.py::test_the_plan_is_validated_against_its_schema`.
- **Done when:**
  - the round-1 schema enumerates exactly the offered read and write names;
  - `propose` is null-only when no write is offered;
  - the round-2 schema has no `reads`;
  - every bad output in the test table is rejected.

### T3 — `ModelPlanner`, round 1

- **Advances:** `REQ-PLN-001`, `002`, `004`, `005`, `006`, `012`
- **Touches:** `engine/plan.py` (`ModelPlanner`, `PlanContext`, `PlanOutcome`,
  `RulePlanner(stand_in=)`); `engine/turn.py` (builds the context, uses the outcome for
  `PLAN`, cost into usage and ceiling); `schemas/common.py` (`Usage.planning_cost_usd`);
  `observability/metrics.py`; `main.py`, `api/deps.py`.
- **Adds test:** in `tests/unit/test_planner.py`:
  - `test_a_turn_with_capabilities_is_planned_by_one_structured_call`;
  - `test_the_planner_prompt_holds_no_identity_and_only_offered_capabilities`;
  - `test_an_invalid_plan_falls_back_and_the_turn_answers`;
  - `test_planning_is_bounded_and_counted_against_the_ceiling`;
  - `test_the_planner_cannot_widen_the_offered_capabilities`;
  - `test_every_planning_call_is_counted`.
- **Done when:**
  - a scripted `orders.list_recent` plan drives one gateway lookup;
  - garbage, a refusal or a timeout drives none, and the turn answers with `PLAN` degraded;
  - `make openapi` shows `planning_cost_usd`.

### T4 — Round 2

- **Advances:** `REQ-PLN-007`, `008`
- **Touches:** `engine/plan.py` (`follow_up`); `engine/turn.py` (one call after reads when
  `needs_results`; the result into the propose branch; detail on `GATHER`).
- **Adds test:** in `tests/unit/test_planner.py`:
  - `test_a_dependent_proposal_takes_exactly_two_rounds`;
  - `test_round_two_arguments_must_validate`.
- **Done when:** "cancel my last order" with scripted rounds sends one lookup, then one
  propose for the looked-up id, with exactly two planning calls.

### T5 — Reporting, no leaks, and the stand-in retired

- **Advances:** `REQ-PLN-010`, `011`, `013`
- **Touches:** `engine/turn.py`, `engine/plan.py`.
- **Adds test:** in `tests/unit/test_planner.py`:
  - `test_the_propose_trigger_is_inert_when_the_planner_is_enabled`;
  - `test_the_plan_stage_reports_what_ran`;
  - `test_the_plan_never_reaches_the_client`.
- **Done when:**
  - every skip reason appears verbatim in a `PLAN` detail;
  - round 2 reports on `GATHER`;
  - no event contains `reason` text or the catalogue;
  - `/propose` is inert with the planner on, even when planning fails.

### T6 — Prompt-log line

- **Advances:** `REQ-PLN-009` (logging half)
- **Touches:** `engine/turn.py` (`planner_prompt` on the turn's log line, beside the channel
  prompt).
- **Adds test:** completes `tests/unit/test_prompts.py::test_the_planner_prompt_is_versioned_and_logged`.
- **Done when:** a planned turn's log carries `planner:v1`.

### T7 — The eval set and `make planner-eval`

- **Advances:** `REQ-PLN-009` (evaluation half)
- **Touches:** `evals/planner/cases.json`, at least 20 cases:
  - order status with and without a number;
  - "the first one" with history;
  - cancel last order (two rounds);
  - cancel a shipped order;
  - small talk;
  - a policy question that needs no lookup;
  - Spanish;
  - an injection attempt in the message;
  - a capability absent from the menu.

  Also `tools/planner_eval.py`, which builds a `ModelPlanner` from `.env`, runs each case
  against air-model-gateway, and reports matches and mismatches; and a `Makefile` target.
- **Adds test:** `tests/unit/test_planner_eval.py`: the runner's scoring on a scripted
  client, so the tool itself is tested hermetically.
- **Done when:**
  - `make planner-eval` runs live against air-model-gateway and prints a per-case table;
  - the result for `cloud` is recorded in this file;
  - mismatches are read and either accepted or turned into prompt changes, as a new
    version;
  - once spec 0014's T2 has landed (a turn can carry a verified customer),
    `make prompt-review LOOKUPS=1` is run against a deployment with the gateway, the planner
    and a token-bearing key. That is the first point a real turn looks something up, so it is
    where spec 0012's two `requires: lookups` conversations are reviewed (0012 T7,
    `REQ-TGW-027`): order facts only from the block, and "could not check" with the gateway
    stopped.

### T8 — Close-out

- **Advances:** all
- **Touches:**
  - `docs/02-lld.md` §1 (`planner_schema.py`), §15;
  - CLAUDE.md: "Real today" (routing is a planner when enabled), and "Where to start";
  - `docs/openapi.json`;
  - spec 0013 §10, and its status.
- **Done when:** `make check` and `make trace` pass, and every spec 0013 row names a passing
  test.

## Status (2026-11-15)

T1–T6 and T8 are done, and T7's runner is built and tested hermetically
(`tests/unit/test_planner_eval.py`, 22 cases in `evals/planner/cases.json`). **T7's live
run has not been made**: `make planner-eval` costs real model calls, so its first run, and
the `cloud` result this file should record, are left for the owner to start. Until then
`planner.enabled` stays `false` everywhere, as T7 requires. The `make prompt-review
LOOKUPS=1` pass (spec 0012 T7) waits on the same run.

## Definition of done

- [x] Every requirement in `spec.md` §5 traces to a passing test, or is a declared `GAP`
- [x] `make check` passes
- [x] `make trace` passes
- [x] `make openapi` run (`Usage.planning_cost_usd`)
- [ ] `make planner-eval` run, results recorded, before `planner.enabled` is set anywhere (not yet run: live and paid; see Status)
- [x] Spec status moved to `Implemented` (or `Partial`, with gaps listed)
- [x] Module docstrings explain the *why*: per-turn enums, two rounds, the fallback without the stand-in
- [x] `docs/02-lld.md` §15 build status updated
