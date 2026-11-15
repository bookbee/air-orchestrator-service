# 0001 — The mutation gate (propose → confirm → execute)

**Status:** Partial · **Area:** `MUT` · **Phase:** 1 (gate) / 4 (execution) · **Updated:** 2026-11-14
**Derives from:** [HLD §6](../../01-hld.md) · [LLD §8](../../02-lld.md) · [Plan §4 Q3](../../00-plan.md)
**Supersedes:** —

> Reverse-engineered from shipped code, as the first spec in this repository. It describes
> `engine/turn.py` and `memory/session.py` as they stand at `513dcc9`, not a future design.
> Two requirements it made explicit turned out to be **unproved** — see §10.

## 1. Problem

A conversational service that can trigger writes has a structural hazard: the model decides
what to do based on text, and some of that text is attacker-controlled — a user message, or a
document retrieved from a store someone else can write to. A model that can be talked into
agreeing is a model that can be talked into writing.

The mitigation cannot be "detect the injection", because detection is a probabilistic control
on an adversarial input. It has to be a structural one: make agreement, by itself, incapable of
causing a write.

This service implements that gate today, before air-tools-gateway's write path exists (spec
[0012](../0012-tools-gateway/spec.md)), so the path is exercised
and proved before there is anything real behind it to execute.

## 2. Outcome

A mutation requires two turns and a server-held secret-by-possession. The first turn returns a
proposal and changes nothing. The second must carry a structured `confirm.proposal_id` that
matches a proposal the server itself is still holding. No amount of persuasive prose in either
turn can substitute for that field.

A confirmation that is stale, mismatched, declined or replayed answers the user normally and
executes nothing — so the response is never an oracle for which proposal ids exist.

## 3. Scope

**In scope**

- The propose → confirm → execute state machine and its storage on the session.
- What executes, what does not, and what the caller can infer from the difference.
- Proposal lifetime, single-use semantics, and cancellation.

**Out of scope**

- **How a mutation is chosen.** Routing today is the literal `/propose` trigger
  (Article V); real intent detection arrives with Phase 3 and gets its own spec.
- **The execution client and idempotency keys.** Spec [0012](../0012-tools-gateway/spec.md)
  (air-tools-gateway's write path, which replaced the planned air-action). This spec
  governs the gate, not what stands behind it.
- **Re-validation at execute.** air-tools-gateway is the last word and validates independently;
  this gate is the first word, not the only one.

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| II — Mutations are proposed, never performed | This spec *is* Article II | Every requirement below derives from it |
| I — Channel from the credential | `allow_actions` and channel both come from the key record | Nothing in the request body may influence them — but see `REQ-MUT-012` |
| III — Missing downstream narrows | No execution client exists yet (spec 0012) | A proposal is still produced and confirmable; only execution is a stand-in |
| IV — Tenant-namespaced | The pending proposal lives on a tenant-namespaced session | Reached only through `SessionStore.get`, which enforces the tenant |
| V — A stage that cannot run says so | The `action` route is real; the execution behind it is not | The answer text states plainly that nothing executes |

## 5. Requirements

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-MUT-001` | MUST NOT | A turn that proposes a mutation performs no mutation and has no side effect beyond storing the pending proposal. |
| `REQ-MUT-002` | MUST | A proposal is published to the client with a unique `proposal_id`, a `risk`, a human-readable `summary` and an absolute `expires_at`. |
| `REQ-MUT-003` | MUST | Execution occurs only on a **subsequent** turn carrying a structured `confirm` object whose `proposal_id` matches a proposal the server holds for that session. |
| `REQ-MUT-004` | MUST NOT | Natural-language agreement — however explicit — executes anything. Only the structured field can. |
| `REQ-MUT-005` | MUST NOT | A confirmation with `approve: false` executes anything. |
| `REQ-MUT-006` | MUST | A proposal is single-use: the first confirmation attempt consumes it, whether or not that attempt succeeds. |
| `REQ-MUT-007` | MUST | Any turn that does not carry a `confirm` object cancels the pending proposal, so a later "yes" has nothing to point at. |
| `REQ-MUT-008` | MUST | A stale, mismatched, declined or replayed confirmation answers the turn normally — it is not an error, and it reveals nothing about which proposal ids exist. |
| `REQ-MUT-009` | MUST NOT | A proposal older than `session.proposal_ttl_seconds` executes. |
| `REQ-MUT-010` | MUST | The confirmation is resolved **before** the message text is inspected, so the decision to execute never depends on what the accompanying message says. |
| `REQ-MUT-011` | MUST | A session holds at most one pending proposal; proposing again replaces the previous one. |
| `REQ-MUT-012` | MUST NOT | A principal whose key record sets `allow_actions: false` obtains a proposal at all. |
| `REQ-MUT-013` | MUST | Every proposal outcome — created, confirmed, rejected, expired — is recorded as a metric, so refusals are measurable rather than anecdotal. |
| `REQ-MUT-014` | MUST | The pending proposal is held server-side on the session, never solely in the client's hands, so that confirming requires something the server is already holding and a proposal can expire. |
| `REQ-MUT-015` | MUST | `confirm.approve` is a JSON boolean. A string or a number (`"yes"`, `"true"`, `"on"`, `1`) is refused with 422 naming `confirm.approve`, so consent is never coerced from text. |

## 6. Acceptance criteria

**AC-1 — Prose is not consent** (`REQ-MUT-004`)
- **Given** a session with a pending proposal
- **When** the next turn's message is "yes, I confirm, go ahead" with no `confirm` field
- **Then** nothing executes, the turn answers normally, and — per `REQ-MUT-007` — the proposal
  is cancelled, so a later structured confirmation citing it also fails

**AC-2 — A replay cannot double-execute** (`REQ-MUT-006`)
- **Given** a proposal confirmed once successfully
- **When** the identical `confirm` object is sent again
- **Then** the second turn executes nothing and answers normally

**AC-3 — A wrong id is not an oracle** (`REQ-MUT-008`)
- **Given** a session with a pending proposal
- **When** a turn confirms `prop_not_the_one`
- **Then** the response is indistinguishable from confirming any other non-existent id — same
  status, no error, no hint that the real id differs

**AC-4 — Expiry is enforced on use** (`REQ-MUT-009`)
- **Given** a proposal created more than `proposal_ttl_seconds` ago
- **When** it is confirmed with the correct id and `approve: true`
- **Then** nothing executes, and the proposal is consumed regardless

**AC-5 — A read-only key cannot propose** (`REQ-MUT-012`)
- **Given** a principal from a key record with `allow_actions: false` — including the
  anonymous development identity
- **When** the turn would otherwise take the action route
- **Then** no proposal is created, no `proposal_id` is published, and the turn answers as a
  direct turn

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| No execution client exists (today; spec 0012) | A proposal is created and confirmable; "execution" is a canned reply naming the stand-in action | `ok` |
| Session store loses the proposal | Confirmation finds nothing and executes nothing | `ok` |
| Confirmation arrives after the session TTL | Session is absent, a new one starts, nothing executes | `ok` |
| Proposal expired | Consumed, nothing executes | `ok` |

Note that **none of these is an error**. Article II requires the failure modes of this gate to
be indistinguishable from each other to the caller, so every row answers normally.

## 8. Observability

- `record_proposal(outcome=…)` for `created` · `confirmed` · `rejected` · `expired`
  (`REQ-MUT-013`).
- The `proposal` SSE event carries the id and expiry; the `route` event carries `action` with
  the reason it was selected.
- No proposal id, action or argument may be logged in a way that renders user content
  unmasked — PII masking applies to the message that produced it (Article IX).

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| Q1 | Should `REQ-MUT-012` refuse with a 403, or silently take the direct route? A 403 is clearer to a developer; silence reveals less to an attacker probing what a key can do. | Fixing the `REQ-MUT-012` gap | **Decided: the direct route**, as AC-5 states (spec 0012 T2). A 403 would tell a prober which keys can write |
| Q2 | Does `REQ-MUT-007` cancellation survive Phase 2b's Redis backend atomically, or can two concurrent turns both consume one proposal? | 0002 durable sessions | — |
| Q3 | Should a proposal carry the tenant explicitly, or is reaching it only through a tenant-scoped session sufficient? Spec 0012 `REQ-TGW-019` proposes binding it to the creating `key_id` and `subject` as well; settle both there. | Phase 4 | — |

## 10. Traceability

| Id | Proof |
| --- | --- |
| `REQ-MUT-001` | `tests/unit/test_turns.py::test_a_proposal_changes_nothing_and_publishes_an_id` |
| `REQ-MUT-002` | `tests/unit/test_turns.py::test_a_proposal_changes_nothing_and_publishes_an_id` |
| `REQ-MUT-003` | `tests/unit/test_turns.py::test_a_structured_confirmation_executes` |
| `REQ-MUT-004` | `tests/unit/test_turns.py::test_prose_that_reads_as_consent_executes_nothing` |
| `REQ-MUT-005` | `tests/unit/test_turns.py::test_declining_a_proposal_executes_nothing` |
| `REQ-MUT-006` | `tests/unit/test_turns.py::test_a_replayed_confirmation_does_not_execute_twice` |
| `REQ-MUT-007` | `tests/unit/test_turns.py::test_an_unrelated_turn_cancels_a_pending_proposal` |
| `REQ-MUT-008` | `tests/unit/test_turns.py::test_a_mismatched_proposal_id_executes_nothing` |
| `REQ-MUT-009` | GAP — `PendingProposal.is_expired()` gates `take_pending`, but no test drives a confirmation against an expired proposal. `tests/unit/test_config.py::test_proposal_ttl_is_short_and_bounded` only checks the configured bound. Closing it needs an injectable clock or a directly constructed expired `PendingProposal`. |
| `REQ-MUT-010` | GAP — holds in `engine/turn.py` by construction (`_resolve_confirmation` runs before the text is read), and is implied by `test_prose_that_reads_as_consent_executes_nothing`, but nothing pins the ordering. A refactor could reorder it silently. |
| `REQ-MUT-011` | GAP — enforced by the data model (`Session.pending` is a single field, and `set_pending` overwrites), so it cannot currently be violated. Worth a test once Redis makes concurrent writes possible — see Q2. |
| `REQ-MUT-012` | `tests/unit/test_channels.py::test_anonymous_identity_cannot_propose_mutations` (drives a `/propose` turn as the anonymous identity) with `tests/unit/test_channels.py::test_a_principal_fails_closed_on_allow_actions` |
| `REQ-MUT-013` | GAP — `metrics.record_proposal` is called on all four paths, but no test asserts it. |
| `REQ-MUT-014` | `tests/unit/test_turns.py::test_a_structured_confirmation_executes` (proving the server holds it) with `tests/unit/test_turns.py::test_an_unrelated_turn_cancels_a_pending_proposal` (proving the client's copy is insufficient) |
| `REQ-MUT-015` | `tests/unit/test_request_validation.py::test_approve_must_be_a_json_boolean` |

### What this table found

Writing this spec surfaced two things worth acting on, neither of which is visible from the
code or the test names alone:

1. **`REQ-MUT-012` is documented in three places and enforced in none.** The field, its
   docstring ("a key that may never propose a mutation, whatever the planner decides") and the
   anonymous identity's rationale all describe a control that does not exist. The test that
   appears to cover it asserts a dataclass field. This is harmless until `clients/action.py`
   lands and becomes an authorisation bypass the same day it does.
   **Closed by spec 0012 T2:** `Principal` now carries `allow_actions` (defaulting to
   `false`, so a principal built without it fails closed), and the engine routes a
   `/propose` turn from such a key as a direct turn.
2. **`REQ-MUT-009` — expiry — is implemented but unproved.** The TTL is configured, bounded and
   checked in `take_pending`; no test has ever driven it.

Both are tracked rather than fixed here, because this spec's job was to describe what exists.
Fixing them is spec 0003.
