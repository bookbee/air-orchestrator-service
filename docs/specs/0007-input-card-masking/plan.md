# 0007 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Implemented · **Updated:** 2026-11-14

## 1. Approach

`guardrails/pii.py` gains one card detector, `_CARD_PATTERN` plus a `_mask_cards(text,
replace)` helper that substitutes only Luhn-valid matches. `mask_input` runs it first, ahead
of `email`, so the phone pattern never sees a card's digits. `scan_output` uses the same
helper with its partial mask (`**** **** **** 1111`) in place of `[CARD]`.

The pattern widens from 13–16 digits to 13–19, the range ISO/IEC 7812 allows. The output
side gains the extra lengths too.

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `guardrails/pii.py` | modified | shared `_CARD_PATTERN` and `_mask_cards`; `credit_card` first in `mask_input` |

## 3. Contract impact

- [ ] No change to public contracts
- [x] Additive only. `[CARD]` is a new token in stored session text, and `credit_card` is a
  new masked category.
- [ ] Breaking

## 4. Configuration

None.

## 5. Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Mask any 13–19 digit run | Order and tracking ids are that long; Luhn spares nine in ten of them |
| A separate input pattern | The bug exists because the two directions had separate tables |

## 6. Test strategy

| Requirement | Test | Approach |
| --- | --- | --- |
| 001, 002, 003, 005 | `test_guardrails_pii.py` | pure function, parametrised over the five SIT formats |
| 004 | `test_turns.py` | full app, a recording gateway, then `GET /v1/sessions/{id}` |
