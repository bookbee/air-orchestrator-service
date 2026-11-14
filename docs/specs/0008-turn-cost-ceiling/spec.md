# 0008 — The turn cost ceiling is enforced before money is spent

**Status:** Implemented · **Area:** `COST` · **Phase:** 2 (ceilings) · **Updated:** 2026-11-14
**Derives from:** [LLD §5 (ceilings)](../../02-lld.md) · spec [0005](../0005-classifier-client/spec.md) ·
SIT bugs OR-002 and OR-003 (air-web-console `bugs/orchestrator/`)
**Supersedes:** —

## 1. Problem

`options.max_cost_usd` is validated and documented as a cap, and the operator's
`turn.max_cost_usd` is documented as a ceiling a caller cannot raise. Neither held. In SIT, a
customer turn with `max_cost_usd: 0.0001` spent $0.0031 and reported `status: ok`. The ceiling
was read only on the business channel, and only *after* the first paid call, to decide whether
a second one could run. A caller's value was used instead of the operator's, not narrowed by
it, so a caller could raise their own ceiling. Separately, air-classifier's spend on the same
turn (often more than the synthesis call) appeared in no `usage` and counted against no
ceiling.

## 2. Outcome

A turn never starts a paid model call that a conservative estimate says would take its spend
past the effective ceiling, on either channel. When the ceiling stops a call, the turn
says so: `status: degraded`, with a `cost ceiling` detail on the stage that was skipped. The
turn's `usage` includes what air-classifier reports spending, and that spend counts against
the turn and session ceilings.

## 3. Scope

**In scope**

- The effective per-turn ceiling, and checking it before each paid call this service makes.
- Counting air-classifier's reported spend in the turn's `usage` and the session total.
- Passing what is left of the ceiling to air-classifier (spec 0005, `REQ-CLS-015`).

**Out of scope**

- **Exact pre-call pricing.** The price table belongs to air-model-gateway, which does not
  publish it and has no per-call cost cap. The estimate here is an upper bound from
  configured rates. When the gateway exposes a per-call cap, the ceiling should be passed
  to it, and this estimate retired.
- **Shrinking `max_tokens` to fit the budget.** That would turn a cost stop into a truncated
  answer, which reads as a bug. A skipped call with an honest status is clearer.
- **The session ceiling's check.** It already runs before any work. Only its input changes
  (`REQ-COST-005`).

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| III — A missing downstream narrows | A ceiling that stops synthesis must not look like an outage | The turn answers with HTTP 200 and `degraded`; no downstream is named, because none failed |
| V — A stage that cannot run says so | A skipped call is a stage that did not run | `synthesise` reports `degraded` with a `cost ceiling` detail, never `ok` |
| VI — Public contracts are closed sets | `usage` gains a field | Additive: `usage.classifier_cost_usd`. No enum changes |
| VII — All model traffic through the gateway | Unchanged | The estimate makes no call |
| I, II, IV, VIII–X | Unaffected | — |

## 5. Requirements

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-COST-001` | MUST | The effective turn ceiling is the lesser of `options.max_cost_usd` and the configured `turn.max_cost_usd`. A caller can lower it and never raise it. |
| `REQ-COST-002` | MUST | Before the prose synthesis call, on both channels, the call is not made if the turn's spend so far plus the call's estimated cost exceeds the effective ceiling. The turn then answers with a fixed text, `status: degraded`, a `synthesise` stage `degraded` whose detail says `cost ceiling`, and no downstream named in `degraded`. |
| `REQ-COST-003` | MUST | The same check runs before the business channel's structured call. When it stops that call, `structured` is `{"answer": <prose>}` and the turn status is unaffected, because the answer itself was given. |
| `REQ-COST-004` | MUST | The estimate is an upper bound for the configured rates: prompt tokens counted from the prompt's characters at `turn.estimate.chars_per_token` (default 3, fewer characters per token than typical text, so more tokens), priced at `turn.estimate.input_usd_per_mtok`, plus the call's whole `max_tokens` at `turn.estimate.output_usd_per_mtok`. |
| `REQ-COST-005` | MUST | air-classifier's reported cost for the turn is added to `usage.cost_usd`, broken out as `usage.classifier_cost_usd`, and added to the session's total, and so it counts against both the turn ceiling and the session ceiling. |
| `REQ-COST-006` | MUST | Spend already made in the turn, the classifier's included, counts toward the check in `REQ-COST-002`. |

## 6. Acceptance criteria

**AC-1 — The SIT repro** (`REQ-COST-002`)
- **Given** a customer turn with `options.max_cost_usd: 0.0001`
- **When** it runs
- **Then** no gateway call is made, `status` is `degraded`, `degraded` is `[]`, the
  `synthesise` detail contains `cost ceiling`, and `usage.cost_usd` is 0

**AC-2 — A caller cannot raise the ceiling** (`REQ-COST-001`)
- **Given** `turn.max_cost_usd` is 0.0001 and the caller sends `max_cost_usd: 10`
- **Then** the turn is stopped as in AC-1

**AC-3 — Classifier spend is visible** (`REQ-COST-005`)
- **Given** air-classifier reports `usage.est_cost_usd: 0.004`
- **Then** `usage.classifier_cost_usd` is 0.004 and `usage.cost_usd` is the gateway's cost plus 0.004

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| Prose call estimated over the ceiling | No call; fixed text; `synthesise` `degraded` (`cost ceiling`) | `degraded` |
| Structured call estimated over the ceiling | No call; `structured` = `{"answer": …}`; logged `turn.cost_ceiling_reached` | unchanged |
| air-classifier reports no `usage` | Its cost counts as 0 | unchanged |

## 8. Observability

- `synthesise` stage detail `cost ceiling: estimated $x, $y left`.
- Log line `turn.cost_ceiling_reached` with the call (`prose` / `structured`), the estimate
  and the ceiling.
- `usage.classifier_cost_usd` on every turn.

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| Q1 | Should air-model-gateway accept a per-call cost cap, so the ceiling is enforced where the price table lives? Cross-repo, tracked for HLD §9. | Retiring `REQ-COST-004` | — |

## 10. Traceability

| Id | Proof |
| --- | --- |
| `REQ-COST-001` | `tests/unit/test_cost_ceiling.py::test_a_caller_cannot_raise_the_ceiling` |
| `REQ-COST-002` | `tests/unit/test_cost_ceiling.py::test_a_prose_call_over_the_ceiling_is_not_made` |
| `REQ-COST-003` | `tests/unit/test_cost_ceiling.py::test_a_structured_call_over_the_ceiling_is_not_made` |
| `REQ-COST-004` | `tests/unit/test_cost_ceiling.py::test_the_estimate_is_an_upper_bound_for_the_configured_rates` |
| `REQ-COST-005` | `tests/unit/test_cost_ceiling.py::test_classifier_spend_is_counted_in_usage_and_the_session` |
| `REQ-COST-006` | `tests/unit/test_cost_ceiling.py::test_classifier_spend_counts_toward_the_synthesis_check` |
