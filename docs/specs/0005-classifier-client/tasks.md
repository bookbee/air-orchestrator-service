# 0005 — Tasks

**Spec:** [`spec.md`](spec.md) · **Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-14

## Order

T1 → T2 → T3.

## Tasks

### T1 — `ClassifierClient`

- **Advances:** `REQ-CLS-003`, `004`, `005`, `008`, `009`
- **Touches:** `clients/classifier.py`
- **Adds test:** `tests/unit/test_classifier_client.py`
- **Done when:** a respx verdict parses; every failure is a `ClassifierError`; the key never renders

### T2 — Engine and wiring

- **Advances:** `REQ-CLS-001`, `002`, `004`, `005`, `006`, `007`, `009`, `010`
- **Touches:** `engine/turn.py`, `api/deps.py`, `main.py`, `api/v1/chat.py`, `api/v1/query.py`
- **Adds test:** `tests/unit/test_classify_stage.py`
- **Done when:** every row of spec §7 is observable on a real turn

### T3 — Integration double

- **Advances:** `REQ-CLS-001`, `003`
- **Adds test:** `tests/integration/test_classifier_wiring.py`
- **Done when:** a double that rejects unknown fields and a wrong key accepts this service's call

## Definition of done

- [x] Every requirement traces to a passing test or a declared `DEFERRED`
- [x] `make check` passes
- [x] `make openapi` run
- [x] Spec status `Partial` — `REQ-CLS-011`–`013` are deferred by design (spec §3)
- [x] LLD §15 updated
