# UAT open items

**As of:** 2026-11-15 · **Build:** `develop` @ `8ed74d7` plus the uncommitted working tree
described below · **`make check`:** 569 passed, 3 skipped; 168 requirements proved, 22
declared gaps, across 15 specs.

This lists what stands between the current build and a UAT deployment. It is a
working list for the next session, not a design document. Behaviour lives in
`docs/specs/` and build status in [LLD §15](02-lld.md). Where this list and either of those
disagree, they win. Strike an item through, with the date, when it is closed.

## What is ready

- Specs 0001–0015 are built and traced. The tools path (specs 0012–0014) has been verified
  end to end against a live air-tools-gateway and air-model-gateway (Gemini) from
  air-web-console: lookups, not found, propose, confirm, execute, a refused change, a
  replayed confirm and a planner failure. Console bugs OR-007 to OR-015 and OR-016b are
  verified fixed.
- Prompt defaults are `customer_care:v5` and `planner:v2`. Earlier versions stay pinnable
  through `prompts.pins`.

## Open items

| # | Item | Blocks UAT? | Owner | Where it is recorded |
| --- | --- | --- | --- | --- |
| 1 | **Commit the working tree.** 34 files are uncommitted, among them the new spec 0015 and `tests/unit/test_entrypoint.py`. | Yes | Product owner (reviews and pushes) | `git status` |
| 2 | **Sessions are in memory.** They are lost on restart, and the service must run as one worker (enforced, spec 0015) and one replica (not enforced: a deployment concern). Durable sessions are Phase 2b, spec 0002, still **Draft**. Its `tasks.md` T0 waits on open questions Q1/Q2. | Yes, unless UAT accepts one replica and losing sessions on restart | Product owner (Q1/Q2), then dev | specs 0002, 0015 |
| 3 | **air-rag-service does not serve.** Line 40 of its `docker-compose.yml` hard-codes `AIR_RAG_SERVICE__MODEL_GATEWAY__API_KEY: air-rag-dev`, overriding its `.env`. Fix that, recreate it seeded with the refund and returns policy, then re-run the item 4 check. | Yes: policy answers have no sources | air-rag-service | this file |
| 4 | **OR-016a: invented policy figures.** With no sources, Gemini states refund times ("5 to 10 business days") and once a fake `[1]` citation. `customer_care:v5` forbids it and does not stop it. *Decided 2026-11-15:* no output guardrail for now; re-check once item 3 is done: `make prompt-review LOOKUPS=1 AIR_TOKEN=… ONLY=refund-time-no-source`, and `returns-question`. If figures persist with sources present, revisit the output check. | Yes | Dev, after item 3 | spec 0006 `tasks.md` |
| 5 | **OR-016c: borrowed dates.** In a session that did not make the cancel, an `orders.get` result's `placed_at` was given as the cancellation date. The prompt rule and the lookups message (`REQ-TGW-027`) did not hold on Gemini. A candidate fix, not yet specified: have the engine relabel or drop date fields that do not describe the current status before lookup results reach the prompt (a spec 0012 amendment). | Yes: a wrong fact told to a customer | Product owner to decide | spec 0006 `tasks.md`; air-web-console `bugs/orchestrator/OR-016-*.md` |
| 6 | **Prompts are reviewed on Gemini only.** Anthropic credit ran out. Re-run `make prompt-review LOOKUPS=1` and `make planner-eval` on the UAT model class before UAT, and record the results in specs 0006 and 0013 `tasks.md`. `REQ-PRM-008` is a declared gap by design. | Yes | Dev | specs 0006, 0013 `tasks.md` |
| 7 | **Two review conversations not run on v5:** `cancel-then-check` and `replayed-confirm`. They need cancellable fixtures: reset air-tools-gateway (55120 and 70011 were cancelled at last check). | Yes (part of item 6) | Dev | spec 0006 `tasks.md` |
| 8 | **"…this time" after a refusal.** On a refused change, v5 adds "I couldn't cancel that order this time", which invites a retry. Fold it into the next prompt version, with a review. | No | Dev | this file |
| 9 | **Planner is off by default** (`planner.enabled=false`). UAT needs it on for the tools path, plus a check of planner latency on the UAT model class. Gemini took 5.5–9.3 s cold against a 4.5 s budget (`deadline_share` 0.3 × 15 s, kept on purpose). A timeout answers safely (`REQ-PLN-017`) but loses that turn's lookups. | Yes (configuration) | Deployment | spec 0013 |
| 10 | **Development credentials.** The `.env.example` keys (`airo_local_*`), the development `hash_salt`, and the customer-token key pair (air-web-console's `.env.example`) are development values. UAT needs its own API keys with `allow_actions` set per key, a real `hash_salt`, and customer-token public keys for the UAT issuer. | Yes | Deployment | `.env.example`, spec 0014 |
| 11 | **"Already cancelled" wording needs a contract change.** air-tools-gateway's `invalid_state` gives its reason only in free text, which is never shown to customers. The proposal: an optional closed-set `state` (`cancelled`, `shipped`, `delivered`) on the `Problem` and on execute's `ExecuteResponse` (`rejected`). It describes the resource the capability acts on; contract version 0.1.0 → 0.2.0. Then a `REQ-TGW-028` amendment maps each value to fixed text. air-tools-gateway reviewed the proposal but has not accepted it. Its owner decides; land it after the gateway's final SIT. | No: the current text is accurate, just generic | Product owner, air-tools-gateway | `docs/specs/0012-tools-gateway/contract.openapi.yaml` (unchanged) |
| 12 | **Spec 0011's grounding check** (`REQ-RAG-014`) is a declared gap. Answers cite sources, but nothing checks that a cited source supports the claim. | Decide for UAT | Product owner | spec 0011 |
| 13 | **Cross-repo:** air-model-gateway's `/v1/generate` has no `tools` field, so the planner uses `json_schema`. This does not block. | No | air-model-gateway | [HLD §9](01-hld.md) |

## Suggested order for the next session

1. Item 1 (commit), then items 3 and 7 (environment), then items 4 and 6 (live reviews with
   sources, on the UAT model class).
2. Bring the decisions in items 2, 5, 11 and 12 to the product owner, each with its
   recommendation.
3. Items 9 and 10 belong in the UAT deployment checklist.
