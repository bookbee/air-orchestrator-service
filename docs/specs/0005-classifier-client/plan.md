# 0005 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Partial · **Updated:** 2026-11-14

## 1. Approach

**`clients/classifier.py`** — `ClassifierClient`, built only when `downstream.classifier` is
configured, so "absent" is `None` on `AppState` rather than a client that always fails. One
method, `classify(text, *, tenant, timeout_s)`, which posts the masked text with
`options.latency_budget_ms` set to the same bound as the HTTP timeout. That way air-classifier's
ladder stops climbing before this service gives up on it, and a slow cloud tier yields a
cheaper verdict rather than a timeout. It returns a narrow `Classification`: only the fields
this service reads, `extra="ignore"`, labels as plain strings. A new label in air-classifier's
vocabulary is not a contract break here. Every failure becomes one `ClassifierError`.

**The engine** — `CLASSIFY` leaves `_STUBBED`. `TurnEngine._classify` decides `skipped`
(unconfigured, business channel), `degraded` (no time left, or `ClassifierError`), or `ok`, and
emits exactly one stage event. The verdict is not passed onward yet (spec §3). Labels reach
`detail` and logs only through a slug check, because they are a downstream's text being
rendered into this service's output.

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `clients/classifier.py` | new | `ClassifierClient`, `Classification`, `ClassifierError` |
| `engine/turn.py` | modified | `_classify`; `CLASSIFY` out of `_STUBBED`; constructor takes the client |
| `api/deps.py`, `main.py` | modified | `AppState.classifier: ClassifierClient \| None`; closed in lifespan |
| `api/v1/chat.py`, `api/v1/query.py` | modified | pass `state.classifier` |

## 3. Contract impact

- [ ] No change to public contracts
- [x] Additive only — `stage{classify}` can now be `ok`/`degraded`; `degraded` may name `air-classifier`
- [ ] Breaking

## 4. Configuration

No new settings. `downstream.classifier.{enabled, base_url, api_key, timeout_ms}` already exist
and default to off.

## 5. Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Validate the response against air-classifier's full enums | A new label upstream would fail every turn's stage here — coupling a deploy order between repos |
| Classify on both channels | Spends classifier tiers on queries whose sentiment drives nothing (spec §3) |
| Run `CLASSIFY` concurrently with synthesis | Its verdict is meant to shape synthesis (`REQ-CLS-012`); running it in parallel would have to be undone then |
| Gate `/v1/ready` on it | An optional capability must not take the service out of rotation (Article III) |

## 6. Test strategy

| Requirement | Test | Approach |
| --- | --- | --- |
| 003, 004, 005, 008, 009 | `test_classifier_client.py` | respx against the client directly |
| 001, 002, 004, 005, 006, 007, 009, 010 | `test_classify_stage.py` | full app with a configured classifier and respx |
| 001, 003 | `tests/integration/test_classifier_wiring.py` | a respx double that enforces `extra="forbid"` and the key |

## 7. Rollout

Off by default. Enabling it is `AIR_ORCHESTRATOR_SERVICE__DOWNSTREAM__CLASSIFIER__ENABLED=true`
plus a key from air-classifier. Nothing user-visible changes until `REQ-CLS-011`/`012` land.

## 8. Risks

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| Classification eats the turn deadline before synthesis | Low (`timeout_ms` 5 s vs a 15 s deadline) | Bounded by `timeout_ms`; the ladder is told the same budget |
| Cloud-tier cost per customer turn | Medium | Clamped by air-classifier's per-key policy; visible in its own metering |
