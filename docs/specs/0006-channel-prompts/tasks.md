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
