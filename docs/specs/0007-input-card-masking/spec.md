# 0007 — Payment cards are masked on input

**Status:** Implemented · **Area:** `PII` · **Phase:** 2 (guardrails) · **Updated:** 2026-11-14
**Derives from:** [HLD §6 (guardrails)](../../01-hld.md) · [LLD §6](../../02-lld.md) ·
SIT bug OR-001 (air-web-console `bugs/orchestrator/`)
**Supersedes:** —

## 1. Problem

Input masking (`mask_input`) replaces email addresses, phone numbers, street addresses and
introduced names with `[CATEGORY]` tokens, and the masked text is what this service stores
in the session, sends to air-classifier, and sends to air-model-gateway, which forwards it to
a cloud provider. It has no rule for payment cards. An unspaced card number passed through in
full. A spaced or dashed one was half caught by the phone rule, which took twelve digits and
left the last group (`[PHONE] 1111`). Either way the primary account number reached storage at
rest and a third party's logs. The output scanner already detects cards (Luhn-checked) and was
never applied to input.

## 2. Outcome

A payment card number in a message, unspaced or grouped with spaces or dashes, in any
issuer's grouping, becomes `[CARD]` before the text is stored or leaves this service. A
digit run that fails the Luhn check, such as an order or tracking id, is left as it was.
Input and output detect cards with the same rule, so the two directions cannot drift apart.

## 3. Scope

**In scope**

- Card numbers of 13 to 19 digits, optionally separated by single spaces or dashes.
- Luhn validation, and the order of rules within input masking.

**Out of scope**

- **CVV codes and expiry dates.** On their own they are three or four digits, or a date,
  and look like ordinary text. Masking them would need the surrounding words, which is the
  phrase-anchored heuristic the name rule uses, and that needs its own false-positive review.
- **Bank account numbers and IBANs.** These are a separate category with country formats.
  A later requirement can add them.
- **Removing PAN from data stored before this change.** The in-memory store holds no data
  across restarts, so there is nothing to backfill.

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| IX — Secrets never render | A card number is not a credential, but it is the most regulated value a customer is likely to type | It is masked before storage and before any downstream call, the same point at which the other PII categories are masked |
| III — A missing downstream narrows | Unaffected | Masking is local and pure |
| I, II, IV–VIII, X | Unaffected | — |

## 5. Requirements

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-PII-001` | MUST | A Luhn-valid number of 13 to 19 digits in an input message, unspaced or grouped by single spaces or dashes, is replaced by `[CARD]` in the masked text, and `credit_card` is listed among the masked categories. |
| `REQ-PII-002` | MUST | Card masking runs before phone masking, so that no part of a card number survives masking as a separate group. |
| `REQ-PII-003` | MUST NOT | A digit run that fails the Luhn check is masked as a card. |
| `REQ-PII-004` | MUST | The text that is stored in the session, sent to air-classifier and sent to air-model-gateway contains `[CARD]` and not the digits. |
| `REQ-PII-005` | MUST | Input masking and output scanning use the same card detection rule. |

## 6. Acceptance criteria

**AC-1 — Every grouping** (`REQ-PII-001`, `002`)
- **Given** the message "My card N was charged twice", for N in `4111 1111 1111 1111`,
  `4111-1111-1111-1111`, `4111111111111111`, `5500 0000 0000 0004` and `3782 822463 10005`
- **When** it is masked
- **Then** the masked text is "My card [CARD] was charged twice"

**AC-2 — An order id** (`REQ-PII-003`)
- **Given** "Order 1234567812345678 is late"
- **Then** the text is unchanged

**AC-3 — Never stored** (`REQ-PII-004`)
- **Given** a customer turn containing `4111 1111 1111 1111`
- **When** the turn completes
- **Then** the session's stored user turn and the gateway request both contain `[CARD]`
  and neither contains `4111`

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| A Luhn-valid number that is not a card (rare: a 13–19 digit id that happens to pass) | Masked as `[CARD]`; the model sees less | unchanged |

Over-masking is the accepted failure direction: the cost is a less specific answer, not a
leaked card.

## 8. Observability

The `guardrails_in` stage does not report input masking today, and this spec does not change
that. The masked categories are on `MaskResult.categories`.

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| — | None open. | — | — |

## 10. Traceability

| Id | Proof |
| --- | --- |
| `REQ-PII-001` | `tests/unit/test_guardrails_pii.py::test_a_card_number_is_masked_on_input` |
| `REQ-PII-002` | `tests/unit/test_guardrails_pii.py::test_a_card_number_is_masked_on_input` |
| `REQ-PII-003` | `tests/unit/test_guardrails_pii.py::test_a_non_luhn_digit_run_is_not_masked_as_a_card` |
| `REQ-PII-004` | `tests/unit/test_turns.py::test_a_card_number_is_never_stored_or_forwarded` |
| `REQ-PII-005` | `tests/unit/test_guardrails_pii.py::test_input_and_output_share_one_card_rule` |
