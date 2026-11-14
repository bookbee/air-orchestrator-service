# 0011 — Customer answers are grounded in air-rag-service's sources, and cite them

**Status:** Partial (`REQ-RAG-014` GAP) · **Area:** `RAG` · **Phase:** 3 (read path) · **Updated:** 2026-11-14
**Derives from:** [HLD §1 (Retrieval client), §4 steps 6–9, §7 (air-rag-service)](../../01-hld.md) ·
[LLD §5](../../02-lld.md) · spec [0006](../0006-channel-prompts/spec.md) (prompts) ·
spec [0008](../0008-turn-cost-ceiling/spec.md) (cost) · spec [0010](../0010-downstream-client-base/spec.md) (client base)
**Supersedes:** —

## 1. Problem

`GATHER` is stubbed, so every answer comes from the model alone. The customer prompt
therefore forbids the model to state any policy ("say the exact terms are on the help
pages"), and the help pages are exactly what customers ask about. air-rag-service can now retrieve
passages from the knowledge base, but nothing calls it. When it does, its passages are text
this service did not write. Placed in a prompt carelessly, they are an injection path into
every customer turn.

## 2. Outcome

On `POST /v1/chat`, `GATHER` asks air-rag-service for passages relevant to the customer's message. The
relevant ones reach the synthesis prompt as numbered, delimited data. The answer is drawn
from them and cites them as `[n]`, and each source the answer cites reaches the caller as a
`citation`. Without air-rag-service, the turn still answers, says it could not check the knowledge
base, and reports `degraded` naming `air-rag`. A passage that tries to instruct the model never
reaches it.

## 3. Scope

**In scope**

- `GATHER` on the customer channel: one retrieval per turn that reaches synthesis.
- How passages reach the prompt, and a new customer prompt version that uses them.
- Citations on both response shapes.
- Retrieval's share of the turn's deadline and cost.

**Out of scope**

- **The business channel.** Analysts ask about data, not help articles. Retrieval there waits
  for a knowledge base that serves them (Q1). `GATHER` reports `skipped` on `/v1/query`.
- **Acting on the classifier's verdict** to decide whether to retrieve. Spec 0005 keeps that
  deferred, so every customer turn that reaches synthesis retrieves.
- **A grounding check on the answer** (are its claims supported by the passages?).
  `answer.grounded` stays `false`. See `REQ-RAG-014`.
- **Running `CLASSIFY` and `GATHER` concurrently.** They stay sequential; parallel fan-out
  comes with air-tools, when there is more than one thing to gather.
- **How air-rag-service namespaces its index.** air-rag-service's concern: each of its keys belongs to one tenant, and it searches only that tenant's documents.
- **Ingestion, chunking, ranking and embeddings.** These belong to air-rag-service (HLD §1).

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| I — Channel from the credential | Retrieval on one channel only | The handler's pinned channel decides. Nothing in a request turns retrieval on or off |
| III — A missing downstream narrows | air-rag-service down | The turn answers, says so, and is `degraded` naming `air-rag` (HLD §7) |
| IV — Tenant-namespaced | A knowledge base shared across tenants would leak one tenant's documents to another | air-rag-service scopes every search to the tenant on the calling key's record, with no fallback. So this service holds one air-rag-service key per tenant and picks it by the principal's tenant. A tenant with no key does not retrieve (`REQ-RAG-016`, `017`) |
| V — A stage that cannot run says so | `GATHER` leaves `_STUBBED` | `ok`, `degraded` or `skipped`, always with a reason when not `ok` |
| VI — Public contracts are closed sets | Citations | `citation` events and `TurnResult.citations` already exist in the contract. They are now populated. `Route.RAG` already exists |
| VII — All model traffic through the gateway | Embeddings | air-rag-service embeds through air-model-gateway itself. This service makes no embedding call |
| IX — Nothing renders secrets | Errors | Through spec 0010's client: no URL, key or body |
| II, VIII, X | Unaffected | — |

Passages are untrusted text, which no article covers directly. `guardrails/boundary.py`'s
rule applies: they are screened, delimited and numbered, and never placed in history
(`REQ-RAG-006`, `007`).

## 5. Requirements

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-RAG-001` | MUST | When air-rag-service is configured, a customer turn that reaches synthesis calls air-rag-service's retrieval once, with the input-masked message and the configured `top_k`, and no field air-rag-service's contract does not define. |
| `REQ-RAG-002` | MUST NOT | air-rag-service never receives the message as the customer typed it when masking changed it. |
| `REQ-RAG-003` | MUST | Turns that never reach synthesis do not retrieve. These are blocked, escalated, session-budget, propose and confirm turns. `GATHER` reports `skipped` with the reason. So does a business-channel turn, and a deployment without air-rag-service. |
| `REQ-RAG-004` | MUST | The call is bounded by the least of air-rag-service's timeout, `deadline_share` (default 0.2) of the turn's deadline, and what is left of the turn. Below `MIN_CALL_MS` it is not made: `GATHER` is `degraded` with a `turn deadline` detail and names no downstream. |
| `REQ-RAG-005` | MUST | At most `max_passages` (default 4) passages are used, each with a score of at least `min_score` (default 0.5), highest first. Each snippet is cut to `snippet_chars` (default 1200). |
| `REQ-RAG-006` | MUST | A passage whose title or snippet the input injection screen would block is dropped, never shown to the model, and counted in the `GATHER` detail. |
| `REQ-RAG-007` | MUST | The passages used reach the synthesis prompt only as delimited data (source `knowledge_base`), numbered `[1]`…`[n]`, after the persona and before the history. A passage cannot close the delimiting tag. Passages are never stored in the session, so a later turn does not see them. |
| `REQ-RAG-008` | MUST | The customer prompt's new version tells the model to answer from the numbered sources when given, to cite each one it uses as `[n]`, to treat the sources as data rather than instructions, and to follow the previous version's rules when there are no sources or they do not cover the question. |
| `REQ-RAG-009` | MUST | After synthesis, one `citation` is emitted for each passage the final answer cites by a valid `[n]`, in order of first citation, before the `answer` event. A passage that is not cited is not listed. `TurnResult.citations` holds the same list. |
| `REQ-RAG-010` | MUST | A citation's `title` is the source's title (or its id), and its `uri` is set only for an `http` or `https` URL. Both are stripped of control characters and cut to a fixed length, because they are a downstream's text rendered to the end user. |
| `REQ-RAG-011` | MUST | When air-rag-service fails (spec 0010's four reasons), `GATHER` is `degraded` naming `air-rag`, and the turn is `degraded`. Synthesis still runs, with a notice in the prompt that the knowledge base could not be consulted, and no citations. |
| `REQ-RAG-012` | MUST | When air-rag-service answers but no passage survives `REQ-RAG-005`/`006`, `GATHER` is `ok` with a `no relevant sources` detail. Synthesis runs without sources, the turn status is unaffected, and nothing is cited. |
| `REQ-RAG-013` | MUST | The passages count in the synthesis call's cost estimate (spec 0008). What air-rag-service reports spending on the call is added to `usage.cost_usd` and the session total, broken out as `usage.retrieval_cost_usd`. |
| `REQ-RAG-014` | SHOULD | The output guardrail checks that the answer's claims are supported by the cited passages, and sets `answer.grounded`. |
| `REQ-RAG-015` | MUST | When retrieval ran, the `route` event lists `rag`, and its `capabilities` include `air-rag`. |
| `REQ-RAG-016` | MUST | When per-tenant air-rag-service keys are configured, retrieval uses the key mapped to the principal's tenant, and never another tenant's key. Since air-rag-service searches only its key's tenant, no passage from another tenant's documents reaches a turn. |
| `REQ-RAG-017` | MUST | When per-tenant keys are configured and the principal's tenant has none, the turn does not retrieve. `GATHER` is `skipped` (`no knowledge base for this tenant`), and the turn status is unaffected. A single unmapped `api_key` is used only when no per-tenant keys are configured, which makes the deployment single-tenant for retrieval. |

## 6. Acceptance criteria

**AC-1 — A grounded answer** (`REQ-RAG-001`, `007`, `009`)
- **Given** air-rag-service returns two passages above `min_score`
- **When** a customer asks "how long do refunds take?" and the answer cites `[2]`
- **Then** the prompt carries both passages, numbered and delimited, and exactly one
  `citation` (the second passage) precedes `answer`

**AC-2 — A poisoned passage** (`REQ-RAG-006`)
- **Given** a passage whose snippet says "ignore previous instructions and …"
- **Then** it is absent from the prompt, and the `GATHER` detail counts one dropped passage

**AC-3 — air-rag-service is down** (`REQ-RAG-011`)
- **Given** air-rag-service refusing connections
- **Then** the turn answers with status `degraded`, `degraded` contains `air-rag`, and the
  prompt tells the model the knowledge base could not be consulted

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| air-rag-service unreachable, timed out, erroring, or breaker open | `GATHER` `degraded` naming `air-rag`; answer says it could not check | `degraded` |
| Too little of the turn left | Not called; `GATHER` `degraded` (`turn deadline`), no downstream named | `degraded` |
| No relevant passage | `GATHER` `ok` (`no relevant sources`); answer without sources | unchanged |
| All passages dropped by the screen | As above, and the detail counts the drops | unchanged |
| air-rag-service reports no usage | Its cost counts as 0. air-rag-service reports none today, and its local embeddings are free | unchanged |

## 8. Observability

- `GATHER` stage detail: `sources=<n> dropped=<m>`, `no relevant sources`, or the failure reason.
- Log line `retrieval.result` with the counts returned, used and dropped, and the top score.
  It never includes passage text.
- `air_orchestrator_service_downstream_calls_total{service="air-rag"}` (spec 0010).

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| Q1 | Should `/v1/query` retrieve, and from which corpus? | Retrieval on the business channel | Product |
| Q2 | Should a turn that the classifier calls small talk skip retrieval? It would save a call and a few hundred prompt tokens. | Nothing: retrieval runs on every customer turn until decided | Product, with spec 0005's deferred `REQ-CLS-011` |

**Decided 2026-11-14 (air-rag-service, bookbee/air-rag-service#1).**

- **Q3: how is retrieval scoped to a tenant?** By air-rag-service's key. Every air-rag-service key record names
  one tenant, retrieval searches only that tenant, and the request body has no tenant field
  (`extra="forbid"`). This service closes `REQ-RAG-016` by mapping each tenant to its own
  air-rag-service key (`REQ-RAG-016`, `017`).
- **Q4: can an archived document outrank the current one?** No longer. air-rag-service keeps
  documents marked `status: archived` indexed but never returns them.

## 10. Traceability

| Id | Proof |
| --- | --- |
| `REQ-RAG-001` | `tests/unit/test_gather_stage.py::test_a_customer_turn_retrieves_once_with_query_and_top_k` |
| `REQ-RAG-002` | `tests/unit/test_gather_stage.py::test_air_rag_receives_the_masked_message` |
| `REQ-RAG-003` | `tests/unit/test_gather_stage.py::test_turns_that_skip_synthesis_do_not_retrieve` · `tests/unit/test_gather_stage.py::test_the_business_channel_does_not_retrieve` |
| `REQ-RAG-004` | `tests/unit/test_gather_stage.py::test_retrieval_gets_a_share_of_the_deadline` · `tests/unit/test_gather_stage.py::test_too_little_time_left_skips_the_call` |
| `REQ-RAG-005` | `tests/unit/test_gather_stage.py::test_only_the_best_passages_above_min_score_are_used` |
| `REQ-RAG-006` | `tests/unit/test_gather_stage.py::test_a_poisoned_passage_never_reaches_the_model` |
| `REQ-RAG-007` | `tests/unit/test_gather_stage.py::test_passages_are_delimited_numbered_and_not_persisted` |
| `REQ-RAG-008` | `tests/unit/test_prompts.py::test_customer_care_v2_grounds_and_cites` |
| `REQ-RAG-009` | `tests/unit/test_gather_stage.py::test_only_cited_passages_become_citations` · `tests/unit/test_gather_stage.py::test_citations_stream_before_the_answer` |
| `REQ-RAG-010` | `tests/unit/test_gather_stage.py::test_citation_fields_are_made_safe` |
| `REQ-RAG-011` | `tests/unit/test_gather_stage.py::test_air_rag_down_degrades_the_turn_and_says_so` |
| `REQ-RAG-012` | `tests/unit/test_gather_stage.py::test_no_relevant_passage_answers_without_sources` |
| `REQ-RAG-013` | `tests/unit/test_gather_stage.py::test_passages_count_in_the_estimate_and_retrieval_cost_in_usage` |
| `REQ-RAG-014` | GAP — needs a grounding check in `guardrails/grounding.py`. Planned as its own spec, after the first SIT round with real passages |
| `REQ-RAG-015` | `tests/unit/test_gather_stage.py::test_the_route_names_rag_when_retrieval_ran` |
| `REQ-RAG-016` | `tests/unit/test_gather_stage.py::test_each_tenant_retrieves_with_its_own_key` |
| `REQ-RAG-017` | `tests/unit/test_gather_stage.py::test_a_tenant_without_a_key_does_not_retrieve` |
