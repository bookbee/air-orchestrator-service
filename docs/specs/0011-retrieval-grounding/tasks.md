# 0011 — Tasks

**Spec:** [`spec.md`](spec.md) · **Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-14

## Order

T1 → T2 → T3 → T4. Needs spec 0010's T1.

## Tasks

### T1 — air-rag-service client

- **Advances:** `REQ-RAG-001`, `010`
- **Touches:** `clients/rag.py`, `config.py`, `api/deps.py`, `main.py`
- **Adds test:** `tests/unit/test_gather_stage.py`
- **Done when:** one retrieval per customer turn, with the contract's fields only

### T2 — `GATHER` in the engine

- **Advances:** `REQ-RAG-002`–`007`, `011`–`013`, `015`
- **Touches:** `engine/turn.py`, `guardrails/boundary.py`, `schemas/common.py`
- **Adds test:** `tests/unit/test_gather_stage.py`
- **Done when:** passages are screened, numbered and delimited, and air-rag-service down degrades the turn and says so

### T3 — Prompt v2 and citations

- **Advances:** `REQ-RAG-008`, `009`
- **Touches:** `prompts/registry.py`, `engine/turn.py`
- **Adds test:** `tests/unit/test_prompts.py`, `tests/unit/test_gather_stage.py`
- **Done when:** only `[n]`-cited passages become `citation` events, before `answer`

### T4 — SIT wiring

- **Advances:** —
- **Touches:** `docker-compose.yml`, `.env.example`
- **Done when:** a live turn against air-rag-service's SIT corpus cites a Brightcart policy

### T5 — Tenant by key

- **Advances:** `REQ-RAG-016`, `017`
- **Touches:** `config.py`, `clients/base.py`, `clients/rag.py`, `engine/turn.py`
- **Adds test:** `tests/unit/test_gather_stage.py`
- **Done when:** each tenant calls air-rag-service with its own key, and an unmapped tenant does not retrieve

## Definition of done

- [x] Every requirement traces to a passing test or a declared `GAP`
- [x] `make check` passes
- [x] `make openapi` run (`Usage` changed)
- [x] `make prompt-review` on `customer_care:v2` with air-rag-service live: 0 flagged, transcripts read. The v1 baseline is spec 0006's run. The `returns-question` expectation was updated for grounded answers
