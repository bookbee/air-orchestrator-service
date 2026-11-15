# 0006 — Tasks

**Spec:** [`spec.md`](spec.md) · **Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-14

## Order

T1 → T2 → T3.

## Tasks

### T1 — Registry, persona, prompts

- **Advances:** `REQ-PRM-002`, `003`, `004`, `005`
- **Touches:** `prompts/registry.py`, `config.py`, `main.py`
- **Adds test:** `tests/unit/test_prompts.py`
- **Done when:** both prompts render from settings; a bad persona or pin fails at startup

### T2 — Engine selection by channel

- **Advances:** `REQ-PRM-001`, `006`
- **Touches:** `engine/turn.py`
- **Adds test:** `tests/unit/test_channel_prompts.py`
- **Done when:** each channel's gateway call carries its own system prompt

### T3 — Review set

- **Advances:** `REQ-PRM-007` (and makes `008` measurable)
- **Touches:** `tools/prompt_review.py`, `evals/prompts/customer_care.json`, `Makefile`
- **Adds test:** `tests/unit/test_prompt_review.py`
- **Done when:** `make prompt-review` runs the set against a live service and reports flags

## Definition of done

- [x] Every requirement traces to a passing test or a declared `GAP`
- [x] `make check` passes
- [x] `make openapi` run
- [x] `make prompt-review` run against the local stack, transcripts read — twice: the first run
  found the persona offering handoffs it cannot make, which became `offers_to_act`
- [x] LLD §5 and CLAUDE.md updated


## Review record — `customer_care:v3` (2026-11-15)

`make prompt-review` with `--lookups` and a verified customer, Gemini (`cloud` class;
Anthropic was out of credit), RAG off, against air-tools-gateway's fixtures. v2 baseline the
same day: `order-not-found` asked for the number it was given, and a confirmed cancel was
later disowned (OR-009). v3:

- `order-not-found`: "I can't find order 99999 on your account", asks to check it.
- `order-status-with-lookup`: names the three orders and asks which; answers the follow-up
  from the data (after the lookups message gained the which-order rule).
- `cancel-then-check`: "Yes, your order 55120 is cancelled."
- `replayed-confirm`: the replay executes nothing and the reply keeps the change.
- Flags: `late-order-frustrated` (`claims_action` for a true "I've checked": the conversation
  predates lookups), `too_long` on a three-order list.
- Open: Gemini once called the placed date the cancellation date, despite the date rule.

The Claude review of v3 is still to run, when Anthropic has credit.

**`make prompt-review LOOKUPS=1`, `customer_care:v4` + `planner:v2`, Gemini, 2026-11-15 (OR-014).**
Run on a :8091 instance (RAG off) against the live air-tools-gateway, whose fixtures were not
fresh: 55120 had already been cancelled by an earlier SIT. The ten conversations that do not
need a cancellable order were run. `cancel-then-check` and `replayed-confirm` wait for a fixture
reset. 0 flags except two:
`late-order-frustrated` "I've checked on order 48213" (`claims_action`, `offers_to_act`). That
is true on a lookup turn, which the lookups message allows (`REQ-TGW-027`). And `cancel-request`
"your order 55120 … has been cancelled", which is true of the stale fixture. No reply said it
can't look things up or change orders (OR-014a). On the first run, `cancel-request` and
`spanish` gave 55120's placed date as its cancellation date (OR-014b) despite v4's date rule
and the lookups message's field-name rule. After the lookups message gained "a status has no
date of its own", neither gave a date in the re-run.

**`customer_care:v5` (OR-016a), Gemini, 2026-11-15 — does not meet `REQ-PRM-008` for figures.**
`refund-time-no-source`, `returns-question`, `late-order-frustrated`, RAG off, three
iterations of v5 on :8091:
- **v5 rule alone.** "5 to 10 working days" twice. The customer prompt's own citation
  example (v2–v4: "Refunds reach your card within 5 to 10 working days [2].") was the
  figure being repeated.
- **Example replaced by a placeholder.** The standalone refund question said "I don't have
  a ballpark figure", but `returns-question` cited "[1]" with no sources given.
- **No example, plus "never write [n] when no numbered sources are given".** "5 to 10
  business days" came back on its own, and `late-order-frustrated` turn 3 gave "within 5
  business days [1]" and an invented "refuse the delivery" route.

v5 stays the default: it is v4's rules plus these, and it no longer carries a figure for the
model to copy. The prompt alone does not stop Gemini inventing figures. *Decided 2026-11-15
(product owner):* no output check for now. OR-016a waits for air-rag-service to serve the real
refund-policy sources, and is re-checked then with `refund-time-no-source`. OR-016c (a
lookup's placed date given as the cancellation date in a session that did not make the
cancel) stays open. Real sources do not address it.
