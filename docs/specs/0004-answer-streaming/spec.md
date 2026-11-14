# 0004 — Streaming the answer (`answer.delta`)

**Status:** Implemented · **Area:** `STRM` · **Phase:** 2a (completes HLD §5) · **Updated:** 2026-11-14
**Derives from:** [HLD §5, §9](../../01-hld.md) · [LLD §4](../../02-lld.md) ·
[Plan §4 "Streaming shape"](../../00-plan.md) · air-model-gateway `docs/04-integration.md`
§Streaming · spec [0003](../0003-model-gateway/spec.md)
**Supersedes:** —

## 1. Problem

`answer.delta` has been defined since v1 and never emitted: air-llm could not stream, so the
plan deferred token streaming to "an air-llm change". air-model-gateway now streams
(`stream: true` on `/v1/generate`), and its migration note names this service as "the one
consumer that benefits". A customer on `/v1/chat` still waits for the whole answer, while
`/v1/capabilities` honestly reports `token_deltas: false`.

Two properties of the gateway's stream make relaying it naïvely unsafe:

- The provider's **refusal verdict arrives last**, in `done`, after the text has streamed; the
  gateway tells consumers to discard the deltas.
- This service's **output guardrail** (PII masking) runs on the finished answer. A delta relayed
  as it arrives would reach the client before the guardrail has seen it — and a phone number or
  card split across two deltas is not recognisable in either.

## 2. Outcome

A streaming caller receives the answer as `answer.delta` events while it is generated. No delta
ever contains text that the output guardrail would redact from the final answer. The `answer`
event remains the authoritative, complete text: on a normal turn it equals the concatenated
deltas; when the provider refuses, the stream fails mid-answer, or the turn deadline expires,
it carries the refusal or fallback text instead, and clients replace what they rendered. A
JSON caller's turn is unchanged.

## 3. Scope

**In scope**

- Relaying the prose synthesis call as `answer.delta`, on both channels, for SSE callers only.
- A streaming output guardrail with a hold-back window.
- Refusal, mid-stream failure, deadline and client-disconnect behaviour.
- `/v1/capabilities` `streaming.token_deltas` reporting the truth.

**Out of scope**

- **Streaming the business channel's `structured` call.** Schema-constrained JSON is only
  usable once complete; it stays a non-streaming call.
- **Streaming for JSON callers.** They get one body; a streaming upstream call would trade
  the gateway's full failover (which it does only before the first token) for nothing.
- **A version bump.** `answer.delta` was reserved for exactly this; clients already ignore
  event names they do not know (Article VI).
- **Customer-only suppression.** Decided 2026-11-14 (product owner): stream on both channels and
  retract on refusal, rather than withhold deltas from end customers.

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| VI — Public contracts are closed sets | Starts emitting an already-defined event; `answer` semantics gain "authoritative over deltas" | Additive; no member renamed. The authority rule is documented on the event model and in OpenAPI |
| III — A missing downstream narrows | A stream that fails after it started | The turn answers with the degraded fallback; it is not an `error` event, because nothing unexpected broke |
| IX — Secrets never render | Deltas are a new output surface | The streaming guardrail redacts `secret`-shaped tokens exactly as the final scan does (`REQ-STRM-003`) |
| VII — All model traffic through the gateway | Uses the gateway's own stream | No other path |
| I, II, IV, V, VIII | Unaffected | Channel still from the credential; proposals never call the model; no keys or stages change |

## 5. Requirements

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-STRM-001` | MUST | An SSE turn that calls the model emits the prose answer as one or more `answer.delta` events, all before the `answer` event. |
| `REQ-STRM-002` | MUST | On a turn that completes normally, the concatenation of `answer.delta` texts equals the `answer` event's text. |
| `REQ-STRM-003` | MUST NOT | Any `answer.delta` contains text that the output guardrail redacts from the final answer — including PII and secrets split across upstream chunks. |
| `REQ-STRM-004` | MUST | When the gateway's `done` carries `refusal: true`, the `answer` event has `refusal: true` and the fixed refusal text, and the turn status is `refused`, whatever deltas preceded it. |
| `REQ-STRM-005` | MUST | When the gateway stream fails after it started (`error` event, dropped connection), the turn answers with the degraded fallback, status `degraded`, naming `air-model-gateway` in `degraded`. |
| `REQ-STRM-006` | MUST | The turn deadline bounds the whole stream, not each read: a stream still running at the deadline is closed and the turn degrades without blaming the gateway. |
| `REQ-STRM-007` | MUST | Closing the turn's stream (client disconnect, deadline) closes the gateway connection, so generation stops upstream. |
| `REQ-STRM-008` | MUST | A JSON (non-SSE) turn makes a non-streaming model call and emits no `answer.delta`. |
| `REQ-STRM-009` | MUST | A stream refused before its first byte (HTTP 4xx/5xx) is classified exactly as the non-streaming call is (`REQ-MGW-007`). |
| `REQ-STRM-010` | MUST | The session stores the authoritative answer text, never the raw stream. |
| `REQ-STRM-011` | MUST | `/v1/capabilities` reports `streaming.token_deltas: true`. |
| `REQ-STRM-012` | MUST | The client parses the gateway's SSE framing and returns usage and cost from `done`, so a streamed turn is metered and capped exactly as a non-streamed one. |

## 6. Acceptance criteria

**AC-1 — A phone number split across chunks** (`REQ-STRM-003`)
- **Given** the model streams `"Call me on 555 12"`, `"3 4567 tomorrow."`
- **Then** no delta contains `555 12` or `3 4567`; the deltas concatenate to the masked answer

**AC-2 — Refused after streaming** (`REQ-STRM-004`)
- **Given** the gateway streams two deltas, then `done` with `refusal: true`
- **Then** the `answer` event is a refusal and `turn.end` is `refused`

**AC-3 — Dropped mid-answer** (`REQ-STRM-005`)
- **Given** the gateway streams a delta, then an `error` event
- **Then** the `answer` is the degraded fallback and `turn.end` is `degraded`

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| Gateway refuses before the first byte (401/403/422/429/502) | As spec 0003, no deltas | `degraded` |
| `error` event / connection lost after `start` | Deltas already sent stand as provisional; `answer` is the fallback | `degraded` |
| `done` with `refusal: true` | `answer` is the refusal text | `refused` |
| Turn deadline reached mid-stream | Gateway stream closed; `answer` is the fallback; gateway not named | `degraded` |
| Client disconnects | Engine and gateway stream cancelled; nothing persisted for the turn | — |
| Redactor's prefix and final scan disagree (should not happen) | Deltas stop; `answer` still complete and redacted | unchanged |

## 8. Observability

- `stage{synthesise}` detail says `streamed` for a streamed call.
- `downstream_calls{service="air-model-gateway",outcome}` counts a streamed call once, at its end.
- `model_gateway.stream_interrupted` log line with the gateway's problem code.

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| — | Customer-channel streaming with refusal retraction — **resolved 2026-11-14**: stream on both channels, retract via the authoritative `answer`. | — | Product owner |

## 10. Traceability

| Id | Proof |
| --- | --- |
| `REQ-STRM-001` | `tests/unit/test_streaming.py::test_deltas_arrive_before_the_answer` |
| `REQ-STRM-002` | `tests/unit/test_streaming.py::test_deltas_concatenate_to_the_answer` |
| `REQ-STRM-003` | `tests/unit/test_guardrails_pii.py::test_stream_redactor_never_releases_split_pii` · `tests/unit/test_guardrails_pii.py::test_stream_redactor_matches_the_final_scan_under_any_chunking` · `tests/unit/test_streaming.py::test_a_phone_number_split_across_deltas_never_streams` |
| `REQ-STRM-004` | `tests/unit/test_streaming.py::test_a_refusal_after_deltas_retracts_them` |
| `REQ-STRM-005` | `tests/unit/test_streaming.py::test_a_mid_stream_error_degrades_the_turn` |
| `REQ-STRM-006` | `tests/unit/test_streaming.py::test_a_stream_past_the_deadline_degrades_without_blaming_the_gateway` |
| `REQ-STRM-007` | `tests/unit/test_model_gateway_client.py::test_closing_a_stream_closes_the_gateway_connection` |
| `REQ-STRM-008` | `tests/unit/test_streaming.py::test_a_json_turn_makes_no_streaming_call` |
| `REQ-STRM-009` | `tests/unit/test_model_gateway_client.py::test_a_stream_refused_before_start_is_classified` |
| `REQ-STRM-010` | `tests/unit/test_streaming.py::test_the_session_stores_the_answer_not_the_raw_stream` |
| `REQ-STRM-011` | `tests/unit/test_system_routes.py::test_capabilities_reports_token_deltas` |
| `REQ-STRM-012` | `tests/unit/test_model_gateway_client.py::test_stream_yields_deltas_then_the_summary` · `tests/unit/test_model_gateway_client.py::test_stream_error_event_raises_after_deltas` |
