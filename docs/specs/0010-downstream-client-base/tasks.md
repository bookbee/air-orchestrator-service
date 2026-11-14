# 0010 — Tasks

**Spec:** [`spec.md`](spec.md) · **Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-14

## Order

T1 → T2 → T3.

## Tasks

### T1 — The shared client and breaker

- **Advances:** `REQ-CLB-001`–`006`
- **Touches:** `clients/base.py`, `config.py`
- **Adds test:** `tests/unit/test_client_base.py`
- **Done when:** three failures open the breaker, and a cancelled call counts as none

### T2 — air-classifier on the base

- **Advances:** `REQ-CLB-004` (through real turns)
- **Touches:** `clients/classifier.py`
- **Adds test:** `tests/unit/test_classify_stage.py`
- **Done when:** every spec 0005 test passes unchanged, and the sixth failing turn makes no request

### T3 — Live status on `/v1/capabilities`

- **Advances:** `REQ-CLB-007`, `008`
- **Touches:** `api/v1/system.py`, `schemas/common.py`, `api/deps.py`, `main.py`
- **Adds test:** `tests/unit/test_system_routes.py`
- **Done when:** air-rag down drops the `rag` route, and `/v1/ready` sends no probe

## Definition of done

- [x] Every requirement traces to a passing test
- [x] `make check` passes
- [x] `make openapi` run (`CapabilitiesResponse` changed)
