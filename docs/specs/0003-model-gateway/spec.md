# 0003 — Model calls through air-model-gateway

**Status:** Implemented · **Area:** `MGW` · **Phase:** 2a (repair) · **Updated:** 2026-11-14
**Derives from:** [HLD §1, §7, §9](../../01-hld.md) · [LLD §6, §15](../../02-lld.md) ·
[Plan §3](../../00-plan.md) · Constitution Article VII (amended 1.1.0) ·
air-model-gateway `docs/06-consumer-migration.md` §air-orchestrator-service
**Supersedes:** —

## 1. Problem

air-llm was retired. Its successor, air-model-gateway, serves the same port (:8083) but has
removed `POST /v1/inference` (now a 404), rejects any request field it does not implement
(`task` is a 422), and no longer maps role aliases such as `generative`. This service still
sends all three, so against the current gateway **every synthesis call fails**.

The failure is invisible. The readiness probe hits the gateway's unauthenticated `/v1/ready`,
which reports healthy, so `/v1/ready` here says ready. Article III then turns every failed
call into a `degraded` turn behind HTTP 200 — and `TurnResult.degraded`, the field meant to
name what was missing, is never populated. An operator sees a ready service with 200s and
canned answers.

## 2. Outcome

Every model call is a `POST /v1/generate` carrying only fields the gateway's contract defines,
naming a model **class** from configuration. When the gateway cannot serve this service — it
is down, it rejects this service's token, or the configured class is not granted or has no
provider behind it — `/v1/ready` says so. When a single call fails, the degraded turn names the
gateway in `degraded`. The gateway's own answer flags (`refusal`, a cut-off `finish_reason`)
are honoured rather than passed through as text. Nothing in this repository still calls the
gateway `air-llm`, in code, configuration, labels or docs.

## 3. Scope

**In scope**

- The client wire contract: path, body, model choice, headers, failure classification.
- Configuration: the `model_gateway` settings section, the model class, and refusing the
  retired `llm` section loudly.
- Readiness that verifies this service can actually be served, not only that the gateway is up.
- Honouring `refusal` and `finish_reason` on the answers the engine already asks for.
- `TurnResult.degraded` populated from the stages that degraded because a downstream failed.
- The rename `air-llm` → `air-model-gateway` throughout: settings, env vars, the
  `DownstreamService` label, module and test names, docs.

**Out of scope**

- **Token streaming.** The gateway can stream; spec [0004](../0004-answer-streaming/spec.md)
  turns that into `answer.delta`. This spec keeps the non-streaming call.
- **Calling air-classifier.** Spec [0005](../0005-classifier-client/spec.md).
- **Which models serve `cloud` for this service, and their order.** The gateway owner's
  decision, in air-model-gateway's `deploy/routing.yaml` `overrides.air-orchestrator-service`
  (Article VII). Its inline comment ("Flash leads for latency") disagrees with its chain
  (Sonnet first); that is reported to the gateway owner, not fixed from here.
- **Retries in this service.** The gateway already retries once and fails over across the
  chain; adding a layer here multiplies cost and latency. The `retryable` flag stays
  informational, as before.
- **Renaming `air-infra`.** That service's repository is now `air-infrastructure-service`, but
  its label is a separate Article VI change with its own consumers; not bundled here.

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| VII — All model traffic through air-model-gateway | This spec implements the amended article | One client, one gateway, a class never a model name; no provider SDK or key added |
| III — A missing downstream narrows | A failed model call | Still a `degraded` 200 with an answer; only readiness and `degraded` become honest. Startup still never requires the gateway |
| VI — Public contracts are closed sets | `DownstreamService.LLM = "air-llm"` is in `/v1/ready` bodies and the `service` metric label | Breaking rename, made here under a spec as the article requires; called out in §8 and in the PR |
| VIII — No module-level singletons | New settings | All in `config.py`, validated; `create_app` still takes settings |
| IX — Secrets never render | The gateway token, now also sent on the readiness probe | Never in a log line, `detail`, repr or exception message — `REQ-MGW-012` |
| II, IV | Unaffected | No mutation path or session key changes |

## 5. Requirements

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-MGW-001` | MUST | Every synthesis call is `POST /v1/generate` on the configured gateway base URL. Nothing calls `/v1/inference`. |
| `REQ-MGW-002` | MUST NOT | A generate request carries any field outside the gateway's `GenerateInput` contract — in particular no `task`. |
| `REQ-MGW-003` | MUST | The `model` sent is the configured model class (`local`, `auto` or `cloud`), defaulting to `cloud` — the class the gateway's migration note assigns to this service. |
| `REQ-MGW-004` | MUST | A configured model value that is not a class (a retired alias such as `generative`, or a model name) fails settings validation, so the service refuses to start rather than 422-ing every call. |
| `REQ-MGW-005` | MUST | Settings under the retired `llm` section (env `AIR_ORCHESTRATOR_SERVICE__LLM__*`) fail settings validation with a message naming the `model_gateway` replacement, rather than being silently ignored. |
| `REQ-MGW-006` | MUST | Every gateway call, the readiness probe included, carries this service's token as `X-API-Key`; a generate call forwards the caller's `X-Request-ID`. |
| `REQ-MGW-007` | MUST | A failed call is classified: connection failure, timeout, 5xx and 429 `rate_limited` are retryable; 429 `budget_exceeded` and every other 4xx are not, and the gateway's problem `code` is kept so a budget stop is distinguishable from a throttle. |
| `REQ-MGW-008` | MUST | A turn whose synthesis call fails still answers with HTTP 200 and `status: degraded`, and its `degraded` list names `air-model-gateway`. |
| `REQ-MGW-009` | MUST NOT | A turn degraded for a reason that is not a downstream failure (its own deadline exhausted before the call) names the gateway in `degraded`. |
| `REQ-MGW-010` | MUST | `/v1/ready` is 503 when the gateway rejects this service's token, not only when the gateway is unreachable. |
| `REQ-MGW-011` | MUST | `/v1/ready` is 503 when the configured class is not both `allowed` for this service and `available` in that gateway deployment. |
| `REQ-MGW-012` | MUST | The readiness probe never raises, and its `detail` never contains a URL, an upstream body, or the token. |
| `REQ-MGW-013` | MUST | The gateway is reported as `air-model-gateway` in `/v1/ready` and in metric labels; `air-llm` appears in no API response. |
| `REQ-MGW-014` | MUST | A gateway answer with `refusal: true` yields an answer with `refusal: true`, a fixed refusal text, and turn status `refused` — never the empty or partial content. |
| `REQ-MGW-015` | MUST | A schema-constrained answer cut off (`finish_reason: length`) or refused yields `structured: null`; the prose answer is unaffected. |
| `REQ-MGW-016` | MUST | The real application, against an HTTP-level double of the gateway's contract, answers a chat turn with the gateway's content. |
| `REQ-MGW-017` | MUST NOT | A settings validation error — the retired-section refusal included — renders any input value. Found while writing `REQ-MGW-005`: pydantic echoed the old token verbatim, and had always echoed whatever settings input failed (the hash salt, inline key records). |

## 6. Acceptance criteria

**AC-1 — The shipped failure, closed** (`REQ-MGW-001`, `REQ-MGW-002`, `REQ-MGW-016`)
- **Given** a gateway double that 404s `/v1/inference` and 422s any unknown body field, exactly
  as air-model-gateway does
- **When** a customer sends a chat turn
- **Then** the turn answers with the double's content and `status: ok`

**AC-2 — A broken credential is not "ready"** (`REQ-MGW-010`)
- **Given** the gateway's `/v1/ready` is 200 but `/v1/capabilities` is 401 for this token
- **When** `/v1/ready` is probed here
- **Then** it is 503, and the gateway's dependency entry says the token was rejected, without
  echoing the token

**AC-3 — A class nobody can serve is not "ready"** (`REQ-MGW-011`)
- **Given** the gateway answers, accepts the token, and reports the configured class with
  `allowed: true, available: false` (no provider key in that deployment)
- **Then** `/v1/ready` is 503 and names the class

**AC-4 — Old configuration fails loudly** (`REQ-MGW-004`, `REQ-MGW-005`)
- **Given** a deployment still setting `AIR_ORCHESTRATOR_SERVICE__LLM__API_KEY`, or
  `…__MODEL_GATEWAY__MODEL_CLASS=generative`
- **Then** settings fail to validate, naming the fix

**AC-5 — The provider said no** (`REQ-MGW-014`)
- **Given** the gateway returns `refusal: true` with empty `content`
- **Then** the answer is a refusal, not "I don't have an answer for that."

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| Gateway unreachable / times out | `/v1/ready` 503; turn answers with the degraded fallback; `degraded: ["air-model-gateway"]` | `degraded` |
| Token rejected (401) / class not granted (403) | `/v1/ready` 503 naming the cause; turns degrade as above; logged at error level as a configuration fault | `degraded` |
| 429 `rate_limited`, 502 | Degraded fallback; retryable | `degraded` |
| 429 `budget_exceeded` | Degraded fallback; not retryable; the code is logged so it reads as a budget stop until UTC midnight | `degraded` |
| 422 (`provider_rejected`, `validation_error`) | Degraded fallback; not retryable; logged at error level — a contract break, not capacity | `degraded` |
| `refusal: true` | Fixed refusal text | `refused` |
| Structured call cut off / refused / fails | `structured: null`; prose answer stands | unchanged |
| Turn deadline spent before the call | Degraded fallback, no call made; gateway **not** named | `degraded` |

## 8. Observability

- `/v1/ready` dependency entry `service: "air-model-gateway"` with a cause in `detail`.
- Metric label `service="air-model-gateway"` on `air_orchestrator_service_downstream_*`.
  **Breaking for dashboards:** queries on `service="air-llm"` stop matching. Called out in the
  change description.
- `model_gateway.call_failed` log line with `status`, `code` (the gateway's problem code) and
  `retryable` — never the URL or body.
- `TurnResult.degraded` and the `stage` event's new optional `downstream` field name the
  service behind a degraded stage.

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| — | None open. The class (`cloud`) is fixed by the gateway owner's migration note; the chain order behind it is theirs (§3). | — | — |

## 10. Traceability

| Id | Proof |
| --- | --- |
| `REQ-MGW-001` | `tests/unit/test_model_gateway_client.py::test_generate_posts_to_v1_generate` |
| `REQ-MGW-002` | `tests/unit/test_model_gateway_client.py::test_generate_body_carries_only_gateway_contract_fields` |
| `REQ-MGW-003` | `tests/unit/test_config.py::test_model_class_defaults_to_cloud` · `tests/unit/test_model_gateway_client.py::test_generate_sends_the_configured_class_as_model` |
| `REQ-MGW-004` | `tests/unit/test_config.py::test_a_retired_model_alias_fails_validation` |
| `REQ-MGW-005` | `tests/unit/test_config.py::test_legacy_llm_settings_fail_validation_naming_the_replacement` |
| `REQ-MGW-006` | `tests/unit/test_model_gateway_client.py::test_the_token_is_sent_as_x_api_key` · `tests/unit/test_model_gateway_client.py::test_the_readiness_probe_sends_the_token` · `tests/unit/test_model_gateway_client.py::test_generate_forwards_the_bound_request_id` |
| `REQ-MGW-007` | `tests/unit/test_model_gateway_client.py::test_budget_exceeded_is_not_retryable_and_keeps_its_code` · `tests/unit/test_model_gateway_client.py::test_rate_limited_is_retryable` · `tests/unit/test_model_gateway_client.py::test_a_422_is_not_retryable` · `tests/unit/test_model_gateway_client.py::test_a_connection_failure_is_classified_retryable` |
| `REQ-MGW-008` | `tests/unit/test_turns.py::test_a_failed_model_call_names_the_gateway_in_degraded` |
| `REQ-MGW-009` | `tests/unit/test_turns.py::test_an_exhausted_deadline_does_not_blame_the_gateway` |
| `REQ-MGW-010` | `tests/unit/test_model_gateway_client.py::test_probe_is_unready_when_the_token_is_rejected` · `tests/integration/test_model_gateway_wiring.py::test_ready_is_503_when_the_gateway_rejects_the_token` |
| `REQ-MGW-011` | `tests/unit/test_model_gateway_client.py::test_probe_is_unready_when_the_class_is_not_available` · `tests/unit/test_model_gateway_client.py::test_probe_is_unready_when_the_class_is_not_allowed` |
| `REQ-MGW-012` | `tests/unit/test_model_gateway_client.py::test_a_connection_failure_never_renders_the_url` · `tests/unit/test_model_gateway_client.py::test_a_non_200_is_unready_with_only_the_status_code` · `tests/unit/test_model_gateway_client.py::test_a_rejected_token_never_renders_the_token` |
| `REQ-MGW-013` | `tests/unit/test_system_routes.py::test_ready_names_the_model_gateway` |
| `REQ-MGW-014` | `tests/unit/test_turns.py::test_a_gateway_refusal_is_a_refused_turn` |
| `REQ-MGW-015` | `tests/unit/test_turns.py::test_a_cut_off_structured_answer_is_dropped` |
| `REQ-MGW-016` | `tests/integration/test_model_gateway_wiring.py::test_a_chat_turn_is_answered_through_the_gateway` |
| `REQ-MGW-017` | `tests/unit/test_config.py::test_a_settings_error_never_renders_a_secret` |
