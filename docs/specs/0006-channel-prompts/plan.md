# 0006 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Partial · **Updated:** 2026-11-14

## 1. Approach

**Prompts are named, not routed.** The registry's key becomes the prompt name, not the route:
`customer_care` and `business_analyst`, each versioned (`v1`). The engine maps channel →
prompt name in one table next to the call. Keying by route stays possible later
(`rag.customer_care`) without another reshape. `direct:v1` is retired, not kept as an alias,
because an alias would let a pin silently select the generic prompt on the customer channel.

**A prompt is a template rendered once per engine.** `Prompt.system` holds `{brand_name}` and
`{who}`. `PromptRegistry.render(name)` fills them from `PersonaSettings`. The persona fields are
validated where they are declared (one plain line, ≤ 60 characters, no `<>{}`), so a value
cannot break the template or the `<data>` boundary that follows it in the same prompt.

**Pins fail at boot.** `create_app` builds a `PromptRegistry` and calls `validate_pins()`
before the server exists. A pin for `direct` names its replacements. That is the one stale pin
an existing deployment can have.

**The review set is data plus a small runner.** `evals/prompts/customer_care.json` holds
multi-turn conversations. `tools/prompt_review.py` drives them through a running service's
`/v1/chat` with the session id carried between turns, prints each transcript, and applies
four deterministic flags (`claims_action`, `offers_to_act`, `too_long`, `no_next_step`).
`offers_to_act` came from the first live run: the persona offered "Would you like me to connect
you?", and a customer's "yes" escalates nothing, because escalation fires only on the customer
asking for a person (`guardrails.escalation`). The flag functions
are pure and unit-tested; the live run is `make prompt-review`.

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `prompts/registry.py` | modified | named prompts, `render`, `validate_pins`; two v1 prompts |
| `config.py` | modified | `PromptSettings.persona: PersonaSettings` |
| `engine/turn.py` | modified | channel → prompt name; a `turn.synthesised` log line with `prompt` |
| `main.py` | modified | `validate_pins()` at boot |
| `tools/prompt_review.py`, `evals/prompts/customer_care.json` | new | live review set |
| `Makefile`, `.env.example` | modified | `prompt-review`; persona variables |

## 3. Contract impact

- [ ] No change to public contracts
- [x] Additive only — `stage{synthesise}` detail text changes (free text); new optional settings
- [ ] Breaking — *only* for a deployment that pinned `direct`, which now fails at boot with
  the replacement named

## 4. Configuration

| Setting | Default | Why it is configurable |
| --- | --- | --- |
| `prompts.persona.brand_name` | `AIR` | The brand the assistant speaks for differs by deployment |
| `prompts.persona.agent_name` | unset | Some brands name their assistant; most deployments will not |

## 5. Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Role text in each message | Lands inside the data boundary, where instructions are correctly ignored; costs tokens every turn; every client must remember it |
| Persona text fully in settings | A prompt is production logic: settings would make it unversioned, unreviewed, and editable without the review set |
| Keep `direct` as an alias | A pin could silently send the generic prompt to customers |
| One prompt with a channel paragraph | Both channels' rules in one prompt invites the model to blend them |

## 6. Test strategy

| Requirement | Test | Approach |
| --- | --- | --- |
| 002, 003, 004, 005 | `test_prompts.py` | registry and settings directly |
| 001, 006 | `test_channel_prompts.py` | full app; capture the `messages` the gateway receives |
| 007 | `test_prompt_review.py` | the pure flag functions and the data file |

## 7. Rollout

The persona is live on deploy; there is no flag. Before shipping a later version, run
`make prompt-review` on both the old and the new and compare. A deployment that pinned
`direct` must change the pin; startup says to what.

## 8. Risks

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| The persona over-apologises or pads replies | Medium | Brevity rules in the prompt; the `too_long` flag |
| It implies a lookup it didn't do ("I've checked…") | Medium | Explicit prohibition; the `claims_action` flag |
| Heuristic flags miss tone problems | Certain | They are a floor; the transcripts are for a human to read |
