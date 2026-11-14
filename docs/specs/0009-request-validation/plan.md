# 0009 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Partial · **Updated:** 2026-11-14

## 1. Approach

**Unknown session (`001`).** `TurnEngine.open(request, principal)` resolves the session and
starts the turn's clock, and raises `SessionNotFoundError` for a `session_id` that does not
resolve. Both routes `await` it before choosing a response shape, so on SSE the error is
raised before the first byte and leaves as a 404 problem, not an `error` event. `run()`
calls `open()` itself if a caller did not, so the engine stays usable on its own.

**Body validation (`003`–`005`).** These are pydantic validators on `ChatRequest.message`,
`QueryRequest.query`, `QueryRequest.output_schema` and `TurnOptions.allow_routes`, so a
violation is an ordinary 422 `validation-error` with a field breakdown. The schema check is
structural and walks the schema recursively: `type`, `properties`, `items`, `required`.

**Auth before body (`006`).** FastAPI parses the body before it resolves dependencies, so
a bad body raises `RequestValidationError` before `require_principal` runs. The validation
handler now checks first whether the matched route depends on `require_principal`. If it
does and the request has no valid key, it answers 401 exactly as `require_principal`
would.

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `engine/turn.py` | modified | `open()`; `run()` uses it |
| `api/v1/chat.py`, `api/v1/query.py` | modified | `await engine.open(...)` before streaming |
| `schemas/chat.py` | modified | blank text, schema shape, `allow_routes` min length |
| `api/errors.py` | modified | 401 before 422 on authenticated routes |

## 3. Contract impact

- [ ] No change to public contracts
- [ ] Additive only
- [x] Breaking, for a caller relying on the old behaviour: an unknown `session_id` was a
  200 with a new session and is now a 404. Blank text, an invalid schema and `allow_routes: []`
  were 200s and are now 422s. `docs/openapi.json` is regenerated.

## 4. Configuration

None.

## 5. Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Keep re-creating, add `session_restarted: true` | Every client must remember to check it; a 404 cannot be missed |
| Parse the body by hand after auth | Loses the generated OpenAPI body schema |
| Add `jsonschema` now | A new runtime dependency is the user's call (spec Q2) |

## 6. Test strategy

`tests/unit/test_request_validation.py`, the full app, both channels and both shapes where they differ.
