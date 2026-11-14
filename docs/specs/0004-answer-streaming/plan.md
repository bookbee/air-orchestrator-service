# 0004 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Implemented · **Updated:** 2026-11-14

## 1. Approach

Three pieces, each with one job.

**`ModelGatewayClient.stream()`** opens `POST /v1/generate` with `stream: true` and returns a
`GatewayStream`. All gateway I/O for that stream happens in **one producer task**, which parses
the SSE frames into a queue; the consumer reads the queue. This is forced by `api/sse.py`,
which drives the engine one `__anext__` at a time from *separate* tasks (so its heartbeat never
stalls): an httpx stream, or an `asyncio.timeout`, spanning those yields would cross tasks.
With a producer task, closing the stream is `task.cancel()`, which exits the httpx context in
the task that entered it and closes the connection — so a disconnect or a deadline stops
generation upstream.

**`guardrails.pii.StreamRedactor`** holds back the tail of the raw text and releases only a
prefix that is (a) at a whitespace boundary, (b) at least `HOLDBACK` characters behind the
newest text, and (c) not straddled by any output-pattern match in the text so far. It releases
`scan_output(prefix).safe_text`, minus what it already released. Every pattern that can span
whitespace (phone, card) is far shorter than the hold-back; every unbounded pattern (email,
JWT, connection string, key) is a single token, released only once a whitespace ends it. If a
release would not extend what was already sent, the redactor stops releasing — the `answer`
event is complete and redacted regardless.

**The engine** streams only when the route says the caller wants SSE
(`TurnRequest.stream`), and only the prose call. Deltas are yielded as they are released; the
`answer` event is built exactly as before from the complete text, so it is authoritative.

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `clients/model_gateway.py` | modified | `stream()`, `GatewayStream`, SSE parsing; `stream_idle_timeout_s` |
| `guardrails/pii.py` | modified | `StreamRedactor` |
| `engine/turn.py` | modified | Streaming prose path; outcome holder; deadline enforced per queue read |
| `api/v1/chat.py`, `api/v1/query.py` | modified | `TurnRequest.stream = wants_stream(accept)` |
| `api/v1/system.py` | modified | `token_deltas: true` |
| `schemas/events.py` | modified | `AnswerDeltaEvent` docstring: provisional; `answer` authoritative |
| `config.py` | modified | `model_gateway.stream_idle_timeout_s` (default 45 s, above the gateway's 30 s idle limit) |

**Not touched:** `api/sse.py` — it already frames any event and already cancels the engine on
disconnect; `collect()` — it already ignores `answer.delta`.

## 3. Contract impact

- [ ] No change to public contracts
- [x] Additive only — `answer.delta` starts being emitted (reserved since v1);
  `token_deltas` flips to `true`
- [ ] Breaking

## 4. Configuration

| Setting | Default | Why it is configurable |
| --- | --- | --- |
| `model_gateway.stream_idle_timeout_s` | 45.0 | A read timeout, not a total: must exceed the gateway's 30 s idle limit so the gateway's own `error` event arrives first |

## 5. Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Relay gateway deltas unmodified | Bypasses the output guardrail; split PII is invisible per chunk |
| Buffer until `done`, then emit | No latency benefit — the product decision was to stream |
| Scan each delta independently | Misses anything split across chunks, which is most phone and card numbers at token granularity |
| `asyncio.timeout` around the stream loop in the engine | The engine's iteration crosses tasks (see §1); the timeout would cancel the wrong task |
| Stream for JSON callers too | Gives up the gateway's pre-first-token failover for no client benefit |

## 6. Test strategy

| Requirement | Test | Fixture / approach |
| --- | --- | --- |
| 003 | `test_guardrails_pii.py` | Deterministic splits plus every split point of a PII-dense text |
| 007, 009, 012 | `test_model_gateway_client.py` | respx streams a real SSE body |
| 001–006, 008, 010 | `test_streaming.py` | Reassign `state.model_gateway.stream` with a fake `GatewayStream`; read the SSE response |
| 011 | `test_system_routes.py` | — |

## 7. Rollout

No flag: a client that ignores `answer.delta` (every existing client, per the contract) sees no
change. A client that renders deltas must treat `answer` as authoritative — stated on the event
model and in the OpenAPI description.

## 8. Risks

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| A refused answer is briefly visible to an end customer | Low (refusals are rare) | Accepted by the product owner; retracted by the authoritative `answer` |
| Hold-back makes the first delta arrive later | Certain, ~64 chars | Small next to model latency; tunable constant |
| Client renders deltas and ignores `answer` | Medium | Documented contract; the refusal test pins the server side |
