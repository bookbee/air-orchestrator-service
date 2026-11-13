# 0004 — Tasks

**Spec:** [`spec.md`](spec.md) · **Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-14

## Order

T1 and T2 are independent. T3 needs both. T4 last.

## Tasks

### T1 — `StreamRedactor`

- **Advances:** `REQ-STRM-003`
- **Touches:** `guardrails/pii.py`
- **Adds test:** `tests/unit/test_guardrails_pii.py::test_stream_redactor_never_releases_split_pii`,
  `::test_stream_redactor_matches_the_final_scan_under_any_chunking`
- **Done when:** every split point of a PII-dense text releases a redacted prefix of the final scan

### T2 — `ModelGatewayClient.stream()`

- **Advances:** `REQ-STRM-007`, `009`, `012`
- **Touches:** `clients/model_gateway.py`, `config.py`
- **Adds test:** the three `test_model_gateway_client.py` nodes in spec §10
- **Done when:** a respx SSE body yields deltas then a summary; closing cancels the producer

### T3 — Engine and routes

- **Advances:** `REQ-STRM-001`, `002`, `004`, `005`, `006`, `008`, `010`
- **Touches:** `engine/turn.py`, `api/v1/chat.py`, `api/v1/query.py`, `schemas/events.py`
- **Adds test:** `tests/unit/test_streaming.py`
- **Done when:** an SSE turn shows deltas then an authoritative answer in every §7 case

### T4 — Capabilities

- **Advances:** `REQ-STRM-011`
- **Touches:** `api/v1/system.py`, `tests/unit/test_system_routes.py`
- **Done when:** `token_deltas` is `true`

## Definition of done

- [x] Every requirement traces to a passing test
- [x] `make check` passes
- [x] `make openapi` run
- [x] Spec status `Implemented`
- [x] HLD §5/§9 and LLD §15 updated
