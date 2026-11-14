# 0008 — Tasks

**Spec:** [`spec.md`](spec.md) · **Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-14

## Order

T1 → T2.

## Tasks

### T1 — Ceiling and estimate

- **Advances:** `REQ-COST-001`–`004`
- **Touches:** `config.py`, `engine/turn.py`
- **Adds test:** `tests/unit/test_cost_ceiling.py`
- **Done when:** the SIT repro (`max_cost_usd: 0.0001`) makes no gateway call and is `degraded`

### T2 — Classifier spend

- **Advances:** `REQ-COST-005`, `006` (with spec 0005 `REQ-CLS-015`)
- **Touches:** `clients/classifier.py`, `schemas/common.py`, `engine/turn.py`
- **Adds test:** `tests/unit/test_cost_ceiling.py`
- **Done when:** a turn's `usage` includes the classifier's reported cost

## Definition of done

- [x] Every requirement traces to a passing test
- [x] `make check` passes
- [x] `make openapi` run (`Usage` changed)
