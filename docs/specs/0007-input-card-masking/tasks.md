# 0007 — Tasks

**Spec:** [`spec.md`](spec.md) · **Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-14

## Tasks

### T1 — One card rule, both directions

- **Advances:** `REQ-PII-001`–`005`
- **Touches:** `guardrails/pii.py`
- **Adds test:** `tests/unit/test_guardrails_pii.py`, `tests/unit/test_turns.py`
- **Done when:** the five SIT formats store `[CARD]`, and the non-Luhn id is untouched

## Definition of done

- [x] Every requirement traces to a passing test
- [x] `make check` passes
