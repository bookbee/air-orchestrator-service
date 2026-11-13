# 0003 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Implemented · **Updated:** 2026-11-14

## 1. Approach

Replace `clients/llm.py` with `clients/model_gateway.py` — same shape (lazy pool behind a lock,
a probe that never raises, a call that raises a classified error), new contract. The engine
keeps one collaborator; only its name, its model argument and the flags it reads change.

Readiness probes two gateway routes concurrently: `GET /v1/ready` (is any provider up?) and
`GET /v1/capabilities` with this service's token (is *this service* served?). The second is the
only cheap call that proves the token, the grant and the class together without spending money
— a one-token `/v1/generate` would prove more but bill on every probe.

This mirrors air-classifier's migration (`ec1d5b5`, then the rename `7c2ef08`), which is the
estate's precedent; where that commit made a choice (class as a validated enum, budget vs
throttle as distinct failures, error-level logging for 4xx), this plan makes the same one.

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `src/air_orchestrator_service/clients/llm.py` | deleted | Replaced, not aliased — a shim would keep `air-llm` alive in imports |
| `src/air_orchestrator_service/clients/model_gateway.py` | new | `ModelGatewayClient`, `GenerateResult`, `GenerateUsage`, `ModelGatewayError` |
| `src/air_orchestrator_service/config.py` | modified | `ModelGatewaySettings` (`model_class`), tombstone `llm` field that fails validation; unused `max_retries`/`default_model` removed |
| `src/air_orchestrator_service/constants.py` | modified | `ModelClass`; `DownstreamService.MODEL_GATEWAY = "air-model-gateway"` replaces `LLM` |
| `src/air_orchestrator_service/engine/turn.py` | modified | Class from settings; refusal and `finish_reason` handling; degraded stages name their downstream |
| `src/air_orchestrator_service/schemas/events.py` | modified | `StageEvent.downstream` (optional, additive) |
| `src/air_orchestrator_service/engine/turn.py::collect` | modified | Fills `TurnResult.degraded` from `downstream` on degraded stages |
| `src/air_orchestrator_service/api/deps.py`, `main.py`, `api/v1/{chat,query,system}.py` | modified | `AppState.model_gateway`; readiness reads the new probe |
| `tests/conftest.py`, `tests/unit/test_llm_client.py` → `test_model_gateway_client.py` | modified / renamed | Fixtures `reachable_model_gateway` / `unreachable_model_gateway` keep the both-directions rule |
| `tests/integration/test_model_gateway_wiring.py` | new | The real app against a respx double that enforces the gateway contract (404 on `/v1/inference`, 422 on unknown fields) |
| `.env.example`, `docker-compose.yml`, `Makefile`, `README.md`, `CLAUDE.md`, `docs/0{0,1,2}-*.md` | modified | Rename; the local `.env.example` uses `local` (Ollama only), production defaults to `cloud` |

**Not touched:** `guardrails/*` (input to the gateway is the same masked text), `prompts/*` (the
prompt does not depend on the provider), `memory/*`.

## 3. Contract impact

- [ ] No change to public contracts
- [x] Additive only — `StageEvent.downstream` (optional); `TurnResult.degraded` now populated
- [x] Breaking — `DownstreamService.LLM` (`"air-llm"`) becomes `MODEL_GATEWAY`
  (`"air-model-gateway"`) in `/v1/ready` bodies and metric labels; env vars
  `AIR_ORCHESTRATOR_SERVICE__LLM__*` become `…__MODEL_GATEWAY__*`. Both are mandated by the
  gateway owner's migration note and by Article VII as amended; the env change fails loudly
  (`REQ-MGW-005`) rather than silently.

`make openapi` regenerates `docs/openapi.json`.

## 4. Configuration

| Setting | Default | Why it is configurable |
| --- | --- | --- |
| `model_gateway.base_url` | `http://localhost:8083` | Per environment (§2 of the gateway's integration guide) |
| `model_gateway.api_key` | none | Per-service, per-environment token; a secret |
| `model_gateway.model_class` | `cloud` | The gateway's assignment for this service; `local` on a laptop with only Ollama |
| `model_gateway.timeout_s` / `health_timeout_s` | 30 / 2 | Unchanged |

## 5. Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Keep `LlmClient` and change two constants | Leaves `air-llm` in every import, fixture and label, which is exactly the drift the gateway owner asked consumers to remove |
| Accept `llm.*` as a deprecated alias of `model_gateway.*` | Two names for one secret is how a rotation updates one and not the other; failing at boot is one deploy's inconvenience |
| Probe readiness with a one-token `/v1/generate` | Proves the provider path too, but bills on every probe, every replica, every few seconds |
| Retry in this client | The gateway already retries and fails over; a second layer multiplies latency and spend |

## 6. Test strategy

| Requirement | Test | Fixture / approach |
| --- | --- | --- |
| 001–003, 006, 007, 010–012 | `test_model_gateway_client.py` | `respx` on a `.invalid` base URL |
| 004, 005 | `test_config.py` | `Settings.model_validate` — no environment mutation |
| 008, 009, 014, 015 | `test_turns.py` | Reassign `state.model_gateway.generate` on the real app |
| 013 | `test_system_routes.py` | `reachable_model_gateway` fixture |
| 010, 016 | `tests/integration/test_model_gateway_wiring.py` | A respx double enforcing the gateway's rules |

## 7. Rollout

One deploy, ordered: set `AIR_ORCHESTRATOR_SERVICE__MODEL_GATEWAY__API_KEY` (the
`air-orchestrator-service-model-gateway-token` secret) and remove the `__LLM__` variables, then
roll out. A pod still carrying `__LLM__` refuses to start and the rollout halts on the old
ReplicaSet — which is the intent. Dashboards on `service="air-llm"` move to
`service="air-model-gateway"` in the same change.

## 8. Risks

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| A deployment manifest still sets `__LLM__*` | Medium | Fails at boot with the replacement named (`REQ-MGW-005`) |
| Readiness flaps on a gateway `/v1/capabilities` blip | Low | Same 2 s health timeout; `/v1/capabilities` is in-process on the gateway, no provider call |
| `cloud` unavailable on a laptop without cloud keys | High locally | `.env.example` sets `local`; readiness says why if not |
