# 0009 — Requests that cannot be answered as asked are refused up front

**Status:** Partial (`REQ-VAL-007` GAP) · **Area:** `VAL` · **Phase:** 2 (hardening) · **Updated:** 2026-11-14
**Derives from:** [LLD §5, §12 (errors)](../../02-lld.md) · SIT bugs OR-004 and OR-005
(air-web-console `bugs/orchestrator/`)
**Supersedes:** —

## 1. Problem

Several requests that cannot be answered as asked were answered anyway, so the caller
learned nothing was wrong:

- A turn naming a `session_id` that does not exist (or has expired) got a 200 with a **new**
  session id. The client believed it had continued a conversation. With the in-memory store,
  that happens to every live conversation on each restart. `GET /v1/sessions/{id}` returns 404
  for the same id, so the two routes disagreed.
- A message of only whitespace passed `min_length=1` and was classified and synthesised, and billed.
- An `output_schema` that is not a JSON Schema (`{"type": "nonsense"}`) was accepted, and the
  structured call could not conform to it.
- `allow_routes: []` was accepted and silently meant "the default".
- A request without a valid key but with malformed JSON got 422, not 401. That tells an
  unauthenticated caller about the route's body before they have proved who they are.

## 2. Outcome

Each of those requests is refused before any stage runs or anything is spent: 404
`session-not-found` for an unknown session, 422 for a blank message, an invalid schema or an
empty route list, and 401 for any request to an authenticated route without a valid key,
whatever its body.

## 3. Scope

**In scope**

- The five cases above, on `POST /v1/chat` and `POST /v1/query`, in both response shapes.

**Out of scope**

- **An unknown or expired `confirm.proposal_id`** (OR-004 part 2). Article II requires such a
  confirmation to be "not an error — the turn answers normally". Changing that is a
  constitution amendment, not a spec. The bug's alternative, a 200 marked `proposal_expired`,
  is the same signal under another name. Recorded as Q1.
- **Probing air-classifier from `/v1/ready`** (OR-005d). Spec 0005 `REQ-CLS-010` and Article
  III keep readiness gated on the gateway alone. Live capability probing is a Phase 3 item.
- **Full JSON Schema metaschema validation.** See `REQ-VAL-007`.

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| IV — Tenant-namespaced | An unknown session is now a 404 on the turn routes | Absent, expired and another tenant's are the same 404, as on `GET` already. A turn route answering 404 reveals nothing `GET` does not |
| II — Mutations are proposed | OR-004 asks for an error on an unknown proposal | Not done; an unknown proposal still answers normally (§3, Q1) |
| III — A missing downstream narrows | Unaffected | These are caller errors, refused before any downstream is called |
| VI — Public contracts are closed sets | New error responses | `session-not-found` and `validation-error` already exist; no new problem types |
| I, V, VII–X | Unaffected | — |

## 5. Requirements

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-VAL-001` | MUST | A turn whose `session_id` names no session this caller can see (never created, expired, deleted, or another tenant's) is refused with HTTP 404 `session-not-found` before any stage runs. On a streaming request it is the response status, not an event. |
| `REQ-VAL-002` | MUST | A turn without `session_id` still starts a new session. |
| `REQ-VAL-003` | MUST | A `message` or `query` that is empty after removing whitespace is refused with 422. |
| `REQ-VAL-004` | MUST | An `output_schema` that is not a JSON Schema object is refused with 422 naming `output_schema`: `type` must be a JSON Schema type name or a list of them, `properties` an object of schemas, `items` a schema, `required` a list of strings. |
| `REQ-VAL-005` | MUST | `options.allow_routes`, when present, has at least one route; `[]` is refused with 422. |
| `REQ-VAL-006` | MUST | A request to a route that requires a key, without a valid key, is refused with 401, whatever its body, malformed JSON included. Body validation errors are reported only to authenticated callers. |
| `REQ-VAL-007` | SHOULD | `output_schema` is checked against the full JSON Schema metaschema. |
| `REQ-VAL-008` | MUST | A body that is not valid JSON is refused, after authentication (`REQ-VAL-006`), with 400 `malformed-request`, not a 422 naming a byte offset as a field. |

## 6. Acceptance criteria

**AC-1 — An unknown session** (`REQ-VAL-001`)
- **Given** no session `sess_doesnotexist`
- **When** a customer sends `{"message": "hi", "session_id": "sess_doesnotexist"}`, as JSON or SSE
- **Then** the response is 404 `session-not-found`, and no model call is made

**AC-2 — Another tenant's session** (`REQ-VAL-001`)
- **Given** a session created by tenant A
- **When** tenant B's key names it
- **Then** the response is the same 404

**AC-3 — Unauthenticated garbage** (`REQ-VAL-006`)
- **Given** no `X-API-Key`
- **When** the body is `{bad`
- **Then** the response is 401, not 422

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| Session store unavailable | Unchanged by this spec (spec 0002 decides) | — |

## 8. Observability

Each refusal is a problem response with its `request_id`, and the access log records its status.

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| — | None open. Both questions below were decided on 2026-11-14. | — | — |

**Decided 2026-11-14.**

- **Q1: should an unknown or expired `confirm.proposal_id` be visible to the caller (OR-004 part 2)?**
  No. Article II stands: such a confirmation answers normally and executes nothing. A proposal is
  scoped to the caller's own session, so the probing risk is small. But the article's rule is
  simple and already proved by tests, and the client that sent the confirm already knows which
  proposal it named. So a client that wants to say "that approval has expired" can do so
  without the service saying it.
- **Q2: add `jsonschema` as a runtime dependency for `REQ-VAL-007`?** No. The structural check in
  `REQ-VAL-004` covers the shapes SIT found, and `REQ-VAL-007` stays a declared gap rather than
  adding a dependency.

## 10. Traceability

| Id | Proof |
| --- | --- |
| `REQ-VAL-001` | `tests/unit/test_request_validation.py::test_an_unknown_session_is_a_404_before_anything_runs` · `tests/unit/test_request_validation.py::test_another_tenants_session_is_the_same_404` |
| `REQ-VAL-002` | `tests/unit/test_turns.py::test_a_turn_without_a_session_id_starts_one` |
| `REQ-VAL-003` | `tests/unit/test_request_validation.py::test_a_blank_message_is_a_422` |
| `REQ-VAL-004` | `tests/unit/test_request_validation.py::test_an_invalid_output_schema_is_a_422` · `tests/unit/test_request_validation.py::test_a_valid_output_schema_is_accepted` |
| `REQ-VAL-005` | `tests/unit/test_request_validation.py::test_an_empty_route_list_is_a_422` |
| `REQ-VAL-006` | `tests/unit/test_request_validation.py::test_an_unauthenticated_bad_body_is_a_401` |
| `REQ-VAL-007` | GAP — a structural check covers the shapes SIT found; the full metaschema needs the `jsonschema` dependency, declined for now (§9, Q2) |
| `REQ-VAL-008` | `tests/unit/test_request_validation.py::test_a_body_that_is_not_json_is_a_malformed_request` |
