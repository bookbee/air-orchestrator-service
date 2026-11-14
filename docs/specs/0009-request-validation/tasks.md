# 0009 — Tasks

**Spec:** [`spec.md`](spec.md) · **Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-14

## Tasks

### T1 — Unknown session is a 404

- **Advances:** `REQ-VAL-001`, `002`
- **Touches:** `engine/turn.py`, `api/v1/chat.py`, `api/v1/query.py`
- **Adds test:** `tests/unit/test_request_validation.py`
- **Done when:** JSON and SSE both answer 404 for `sess_doesnotexist`

### T2 — Body checks and auth order

- **Advances:** `REQ-VAL-003`–`006`
- **Touches:** `schemas/chat.py`, `api/errors.py`
- **Adds test:** `tests/unit/test_request_validation.py`
- **Done when:** the OR-005 a, b, c and e repros are refused as specified

## Definition of done

- [x] Every requirement traces to a passing test or a declared `GAP`
- [x] `make check` passes
- [x] `make openapi` run
