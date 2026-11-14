# 0008 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Implemented · **Updated:** 2026-11-14

## 1. Approach

**Settings.** `TurnSettings.estimate` (`CostEstimateSettings`) holds the rates the estimate
prices at: `input_usd_per_mtok` 3.00, `output_usd_per_mtok` 15.00, and `chars_per_token` 3.
The rates are the list prices of the most expensive model the `cloud` class reaches in
air-model-gateway's `deploy/pricing.yaml`. A deployment that routes to a dearer model
must raise them, because an under-estimate overspends silently.

**Engine.** `TurnEngine._turn_ceiling` is `min(option, setting)`. The engine keeps the turn's
spend in a `_Spend` holder: the classifier's reported cost after `CLASSIFY`, and each gateway
call's cost as it returns. `_estimate(messages, max_tokens)` prices a call before it is made.
`_answer` raises `_CostCeiling` instead of making the prose call. `_pipeline` catches it the
way it catches `TimeoutError`: a degraded `synthesise` stage, a fixed answer, and no
downstream named. The structured call's check replaces the old post-hoc `>=` test.

**Usage.** `Usage.classifier_cost_usd` is new. `_finish_with_answer` folds the classifier's
cost into `usage` on every path, so a turn degraded after `CLASSIFY` still reports it, and
into `session.total_cost_usd`.

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `config.py` | modified | `CostEstimateSettings`, `TurnSettings.estimate` |
| `engine/turn.py` | modified | `_turn_ceiling`, `_estimate`, `_Spend`, `_CostCeiling` |
| `schemas/common.py` | modified | `Usage.classifier_cost_usd` |
| `clients/classifier.py` | modified | `Classification.usage` (spec 0005) |

## 3. Contract impact

- [ ] No change to public contracts
- [x] Additive only. `usage.classifier_cost_usd` is new, and `usage.cost_usd` now includes
  the classifier's spend. A turn can now be `degraded` with an empty `degraded` list because of
  its cost ceiling, which `REQ-MGW-009` already allows for its deadline.
- [ ] Breaking

## 4. Configuration

`AIR_ORCHESTRATOR_SERVICE__TURN__ESTIMATE__{INPUT_USD_PER_MTOK,OUTPUT_USD_PER_MTOK,CHARS_PER_TOKEN}`.

## 5. Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Enforce after the call (the old behaviour) | The money is already spent; the cap is decoration |
| Ask the gateway for prices | It publishes none; adding that is a cross-repo change (spec Q1) |
| Shrink `max_tokens` to fit | Turns a cost stop into a truncated answer (spec §3) |

## 6. Test strategy

`tests/unit/test_cost_ceiling.py`: the full app, a gateway stub that records calls and
prices them, and a respx classifier double that reports usage.
