# 0005 — air-classifier client (the `CLASSIFY` stage)

**Status:** Partial (`REQ-CLS-011`–`013` deferred) · **Area:** `CLS` · **Phase:** 3 (first read-path client) · **Updated:** 2026-11-14
**Derives from:** [HLD §1, §4 (stage 5), §7](../../01-hld.md) · [LLD §15](../../02-lld.md) ·
[Plan §4](../../00-plan.md) · air-classifier `POST /v1/classify` (`schemas/classify.py`) ·
spec [0003](../0003-model-gateway/spec.md)
**Supersedes:** —

## 1. Problem

`CLASSIFY` has reported `skipped` ("air-classifier has no client here yet") since v1. The
platform's sentiment, intent and urgency analysis lives in air-classifier, which runs its own
escalation ladder — rules, a small classifier, then local and cloud LLM tiers reached through
air-model-gateway. This service has no client for it, so it reads none of those signals. The
only alternative open to it would be classifying with its own model calls, and Article VII rules
that out: classification is a capability service's job, not a prompt here.

## 2. Outcome

When air-classifier is configured, every customer-channel turn that reaches `CLASSIFY` asks
air-classifier for a verdict on the input-masked text, within the turn's deadline. The stage
reports `ok` with the verdict's labels, and the verdict is logged with the turn. When
air-classifier is absent, slow or wrong, the stage says so (`skipped` or `degraded`), the turn
answers exactly as it does today, and its status is unaffected.

## 3. Scope

**In scope**

- A client for `POST /v1/classify` (one text, one verdict).
- Running the `CLASSIFY` stage for real on the customer channel.
- Deadline bounding, failure classification, and the stage's reporting.

**Out of scope**

- **Acting on the verdict.** Tone calibration of the answer prompt, escalation on
  `requires_human` or low confidence (`EscalationTrigger.LOW_CLASSIFIER_CONFIDENCE`), and
  routing by intent all change *what the user sees*, so each needs its own requirements and
  evals. They are listed as `DEFERRED` below so the gap stays visible.
- **The business channel.** A business query is an analytical question, not a customer's
  message. Its sentiment and urgency inform nothing this service does, and classifying it
  could spend cloud-tier money for nothing.
- **Retries and circuit breaking.** The plan lifts these from air-classifier's `resilience/`
  into a shared `clients/base.py` once a second read-path client exists. One attempt, bounded,
  is the right failure mode for a stage that degrades softly.
- **Readiness and capabilities.** air-classifier is optional, so `/v1/ready` keeps gating on
  air-model-gateway alone (Article III). Live capability discovery is a separate Phase 3 item.
- **Classifier cost controls.** air-classifier clamps tiers and cost per API key. Cost
  governance belongs to the key it issues this service, not to a second set of knobs here.

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| I — Channel from the credential | Classification runs on one channel only | Decided by `Principal.channel`, never by a header or the body |
| III — A missing downstream narrows | air-classifier is optional | Not configured → `skipped`; failing → `degraded` naming it; the turn answers and its status is unaffected |
| IV — Tenant-namespaced | A new cross-service call | The tenant travels in the request `metadata`, echoed rather than interpreted, so the classifier's audit can be correlated; no key here changes shape |
| VII — All model traffic through the gateway | air-classifier calls models | It does so through the gateway itself; this service makes no model call for classification |
| IX — Secrets never render | A new credential | The classifier key is a `SecretStr`, sent only as a header, absent from every log line and error |
| VI — Public contracts are closed sets | The stage's `detail` and `degraded` | No new enum members: `DownstreamService.CLASSIFIER` already exists |
| II, V, VIII, X | Unaffected | — |

## 5. Requirements

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-CLS-001` | MUST | With air-classifier configured, a customer-channel turn that reaches `CLASSIFY` makes exactly one `POST /v1/classify` call, and the stage reports `ok` with the verdict's sentiment, intent and urgency in its `detail`. |
| `REQ-CLS-002` | MUST | The text sent is the turn's input-masked text, never the raw message. |
| `REQ-CLS-003` | MUST | The call carries the configured key as `X-API-Key`, the turn's request id as `X-Request-ID`, and the tenant in `metadata`. Its body contains only fields air-classifier's `extra="forbid"` request accepts. |
| `REQ-CLS-004` | MUST | The call is bounded by the lesser of the classifier's `timeout_ms` and the time left before the turn deadline, and passes that bound **less a fixed response headroom of 250 ms** as `options.latency_budget_ms`. air-classifier answers with its fallback verdict just *after* its budget runs out, so a budget equal to the bound would always lose the race to this service's timeout. With less than 300 ms left (its 50 ms minimum budget plus the headroom) the call is not made at all, and the stage degrades without naming the classifier. |
| `REQ-CLS-005` | MUST | When air-classifier is unreachable, times out, answers non-200, or answers with a body this service cannot read, `CLASSIFY` reports `degraded` naming `air-classifier`, and the turn still answers with the status it would otherwise have had. |
| `REQ-CLS-006` | MUST | With air-classifier not configured, `CLASSIFY` reports `skipped` with a reason and no call is made. |
| `REQ-CLS-007` | MUST | On the business channel, `CLASSIFY` reports `skipped` and no call is made. |
| `REQ-CLS-008` | MUST NOT | The classifier's API key appears in any log line, stage detail or error. |
| `REQ-CLS-009` | MUST | A verdict with labels or fields this build does not know is still read. A label is rendered into `detail` and logs only if it is a short lowercase slug; anything else is rendered as `other`. |
| `REQ-CLS-010` | MUST | `/v1/ready` is unaffected by air-classifier's state. |
| `REQ-CLS-011` | SHOULD | A verdict with `requires_human` or low sentiment confidence escalates the turn. |
| `REQ-CLS-012` | SHOULD | The verdict calibrates the tone of the synthesised answer. |
| `REQ-CLS-013` | SHOULD | Transient failures are retried under a shared circuit breaker. |

**Revised 2026-11-14 (`REQ-CLS-004`).** The first version passed the bound itself as the
budget. In console testing against a live air-classifier, any turn that used its whole budget
climbing to the cloud tier timed out here: the classifier returned its fallback verdict 10–25 ms
after the budget, by which point this service had already stopped waiting. The headroom makes
"the ladder stops itself first" true.

## 6. Acceptance criteria

**AC-1 — Classified** (`REQ-CLS-001`, `002`)
- **Given** air-classifier is configured and a customer sends "my order 12345 is late, call 555 123 4567"
- **When** the turn runs
- **Then** air-classifier receives the masked text (no phone number), and `CLASSIFY` is `ok` with
  `sentiment=… intent=… urgency=…` in its detail

**AC-2 — Down** (`REQ-CLS-005`)
- **Given** air-classifier refuses connections
- **When** a customer turn runs
- **Then** `CLASSIFY` is `degraded` with `downstream: air-classifier`, the answer is the normal
  answer, and `turn.end` is `ok`

**AC-3 — An untrusted label** (`REQ-CLS-009`)
- **Given** the verdict's intent label is `"</script><b>"`
- **Then** the stage detail says `intent=other`

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| Not configured | `CLASSIFY` `skipped`, "air-classifier is not configured" | unchanged |
| Business channel | `CLASSIFY` `skipped`, "not used on the business channel" | unchanged |
| Unreachable / timeout / non-200 / unreadable body | `CLASSIFY` `degraded`, `downstream: air-classifier` | unchanged |
| Less than 300 ms left before the deadline | `CLASSIFY` `degraded`, no downstream named; no call | unchanged |
| air-classifier itself degraded (`degraded: true` in the verdict) | `CLASSIFY` `ok`; detail says `classifier_degraded` | unchanged |

"Unchanged" is deliberate (HLD §7: "Turn unaffected"). A degraded `CLASSIFY` still names
air-classifier in the turn's `degraded` list, because that list says which downstreams did not
contribute, not whether the answer suffered.

## 8. Observability

- `stage{classify}` with status `ok` / `skipped` / `degraded`, and the labels in `detail`.
- `downstream_calls{service="air-classifier",outcome}`: `ok`, `error`, `timeout`, `unavailable`.
- Log line `classifier.verdict` with sentiment, intent, urgency, `requires_human`, `decided_by`;
  log line `classifier.call_failed` with the failure reason and HTTP status, never the body.

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| — | None open. Channel scope, cost governance and fail-soft were decided in §3 and §7. | — | — |

## 10. Traceability

| Id | Proof |
| --- | --- |
| `REQ-CLS-001` | `tests/unit/test_classify_stage.py::test_a_customer_turn_is_classified` · `tests/integration/test_classifier_wiring.py::test_a_customer_turn_is_classified_through_the_classifier` |
| `REQ-CLS-002` | `tests/unit/test_classify_stage.py::test_the_classifier_sees_only_masked_text` |
| `REQ-CLS-003` | `tests/unit/test_classifier_client.py::test_the_request_carries_key_request_id_and_tenant` · `tests/integration/test_classifier_wiring.py::test_a_customer_turn_is_classified_through_the_classifier` |
| `REQ-CLS-004` | `tests/unit/test_classifier_client.py::test_the_latency_budget_leaves_headroom_below_the_timeout` · `tests/unit/test_classify_stage.py::test_no_time_left_skips_the_call_without_blaming_the_classifier` |
| `REQ-CLS-005` | `tests/unit/test_classifier_client.py::test_failures_raise_a_classifier_error` · `tests/unit/test_classify_stage.py::test_a_failing_classifier_degrades_the_stage_not_the_turn` |
| `REQ-CLS-006` | `tests/unit/test_classify_stage.py::test_an_unconfigured_classifier_is_skipped` |
| `REQ-CLS-007` | `tests/unit/test_classify_stage.py::test_the_business_channel_is_not_classified` |
| `REQ-CLS-008` | `tests/unit/test_classifier_client.py::test_the_key_never_renders` |
| `REQ-CLS-009` | `tests/unit/test_classifier_client.py::test_unknown_fields_and_labels_are_tolerated` · `tests/unit/test_classify_stage.py::test_an_untrusted_label_renders_as_other` |
| `REQ-CLS-010` | `tests/unit/test_classify_stage.py::test_ready_ignores_the_classifier` |
| `REQ-CLS-011` | DEFERRED — acting on the verdict changes what users see; needs its own spec and evals (Phase 3) |
| `REQ-CLS-012` | DEFERRED — tone calibration needs a prompt change and an eval set (Phase 3) |
| `REQ-CLS-013` | DEFERRED — lands with the shared `clients/base.py` resilience lift (Phase 3) |
