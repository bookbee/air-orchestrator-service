# 0011 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Partial · **Updated:** 2026-11-14

## 1. Approach

**`clients/rag.py`.** `RagClient(DownstreamClient)` has one method, `retrieve(query, *,
tenant, timeout_s)`. It posts `{"query", "top_k"}` to `/v1/retrieve`, which is air-rag's whole
request contract, with the tenant's air-rag key (`RagSettings.key_for`), and returns a loose `Retrieval`: `citations` as `Passage` (source_id, snippet,
score, metadata), and an optional `usage.est_cost_usd` that air-rag does not send yet.
`safe_title()` and `safe_uri()` clean the two metadata fields before they reach a caller.

**The engine.** `GATHER` leaves `_STUBBED`. The route is decided before it: `rag` when
air-rag is configured, the channel is customer, and the turn will be synthesised (not a
proposal or a valid confirmation). `_gather()` emits one stage event. It is `skipped` with a
reason, `degraded` for too little time (no downstream named) or a `RagError` (air-rag named;
turn `degraded`), or `ok`. On `ok`, `_select()` sorts by score, stops at `min_score` or
`max_passages`, cuts each snippet, and drops any passage whose snippet or title
`screen_input()` would block. The result is held on the engine for this request:
`_sources`, `_kb_unavailable`.

**The prompt.** `_prose_messages()` adds a second system message after the persona. It
carries either the sources header plus one `delimit(..., source="knowledge_base")` block of
`[n] title\nsnippet` entries, or the fixed "knowledge base could not be consulted" notice.
Both the call and its cost estimate use these messages, so the passages count against the
ceiling with no extra code. `delimit()` now escapes `<data` / `</data` inside the content,
so no passage (or customer message) can close the block. `customer_care:v2` adds the source
and citation rules, and becomes the latest version.

**Citations.** `_finish_with_answer()` runs `_citations()` on the guarded final text. It finds
the valid `[n]` markers, in order of first use, without duplicates, and emits a
`CitationEvent` for each before `AnswerEvent`. `collect()` already folds those into
`TurnResult.citations`. Fallback texts carry no markers, so every path shares this code.

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `clients/rag.py` | new | `RagClient`, `Passage`, `Retrieval`, `safe_title`, `safe_uri` |
| `config.py` | modified | `RagSettings`: `deadline_share` 0.2, `top_k` 6, `max_passages` 4, `min_score` 0.5, `snippet_chars` 1200 |
| `engine/turn.py` | modified | `_gather`, `_select`, `_sources_message`, `_citations`; route decision; retrieval cost in usage |
| `guardrails/boundary.py` | modified | escapes `data` tags inside content |
| `prompts/registry.py` | modified | `customer_care:v2`, latest |
| `schemas/common.py` | modified | `Usage.retrieval_cost_usd` |
| `api/deps.py`, `main.py`, `api/v1/chat.py`, `api/v1/query.py` | modified | `AppState.rag`; passed to the engine |
| `docker-compose.yml`, `.env.example` | modified | air-rag over the host (it is not on air-net); its settings |

## 3. Contract impact

- [ ] No change to public contracts
- [x] Additive only: `citation` events and `TurnResult.citations` are now populated;
  `usage.retrieval_cost_usd`; `stage{gather}` can be `ok`/`degraded`; `degraded` may name
  `air-rag`; the `route` event can name `rag`. All of these were already in the contract's
  vocabulary
- [ ] Breaking

## 4. Configuration

`AIR_ORCHESTRATOR_SERVICE__DOWNSTREAM__RAG__{ENABLED, BASE_URL, API_KEY, TIMEOUT_MS,
DEADLINE_SHARE, TOP_K, MAX_PASSAGES, MIN_SCORE, SNIPPET_CHARS, TENANT_KEYS}`. `TENANT_KEYS` is JSON, tenant to
air-rag key; when it is set, it is the only source of keys. `MIN_SCORE` 0.5 comes from
air-rag's measurements on the SIT corpus with local-768-v1: relevant top hits scored 0.55 to
0.81, off-topic ones 0.39 to 0.48.

## 5. Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Cite every passage given to the model | The `Citation` contract says only sources that contributed; listing all of them measures recall, not grounding |
| A `tenant` body field | air-rag scopes by key and forbids unknown fields. A per-tenant key map (`tenant_keys`) is the only scoping it honours (`REQ-RAG-016`) |
| Fall back to `api_key` for a tenant missing from `tenant_keys` | That key belongs to some tenant, so the fallback would serve one tenant's documents to another (`REQ-RAG-017`) |
| A fixed "cannot check" answer when air-rag is down | Turns that need no knowledge base ("hi", "thanks") would fail too. HLD §7 asks the answer to say so, not to refuse |
| Passages in the user message | They would sit next to the customer's text and in the history; a system message for this turn only is neither |
| Delimit each passage separately | The boundary instruction would repeat n times; one block with numbered entries is clearer and shorter |

## 6. Test strategy

`tests/unit/test_gather_stage.py` drives the full app with air-rag as a respx route and a
recording gateway, on both response shapes. `test_prompts.py` covers v2. A live SIT round
against air-rag's `make run-sit` corpus follows, and `make prompt-review` runs on
`customer_care:v2` (live, paid; spec 0006).
