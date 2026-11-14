# 0003 — Tasks

**Spec:** [`spec.md`](spec.md) · **Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-14

## Order

T1 → T2 → T3 → T4 → T5. T1 (settings and constants) is what every later task imports; T5 (the
rename in docs and deployment files) lands last so the docs describe code that exists.

## Tasks

### T1 — Settings and constants

- **Advances:** `REQ-MGW-003`, `REQ-MGW-004`, `REQ-MGW-005`
- **Touches:** `config.py`, `constants.py`
- **Adds test:** `tests/unit/test_config.py::test_model_class_defaults_to_cloud`,
  `::test_a_retired_model_alias_fails_validation`,
  `::test_legacy_llm_settings_fail_validation_naming_the_replacement`
- **Done when:** `Settings(llm=…)` and `model_class="generative"` both fail validation

### T2 — `clients/model_gateway.py`

- **Advances:** `REQ-MGW-001`, `002`, `006`, `007`, `010`, `011`, `012`
- **Touches:** `clients/model_gateway.py` (new), `clients/llm.py` (deleted)
- **Adds test:** `tests/unit/test_model_gateway_client.py` (all the client nodes in §10)
- **Done when:** a respx double enforcing the gateway's contract is satisfied

### T3 — Engine, wiring, readiness

- **Advances:** `REQ-MGW-008`, `009`, `013`, `014`, `015`
- **Touches:** `engine/turn.py`, `schemas/events.py`, `api/deps.py`, `main.py`, `api/v1/*.py`,
  `tests/conftest.py`
- **Adds test:** the `test_turns.py` and `test_system_routes.py` nodes in §10
- **Done when:** `make check` is green with the renamed fixtures

### T4 — Integration test against a gateway double

- **Advances:** `REQ-MGW-010`, `REQ-MGW-016`
- **Touches:** `tests/integration/` (new)
- **Adds test:** `tests/integration/test_model_gateway_wiring.py::*`
- **Done when:** the double 404s `/v1/inference` and 422s unknown fields, and a turn still answers

### T5 — Rename everywhere else

- **Advances:** `REQ-MGW-013` (no `air-llm` left in responses), the Article VII amendment
- **Touches:** `.env.example`, `docker-compose.yml`, `Makefile`, `README.md`, `CLAUDE.md`,
  `docs/00-plan.md`, `docs/01-hld.md`, `docs/02-lld.md`, module docstrings, `docs/openapi.json`
- **Adds test:** none — `make openapi` and `grep -ri air-llm` are the check
- **Done when:** the only remaining mentions of air-llm are historical (the amendment log, this
  spec's problem statement)

## Definition of done

- [x] Every requirement in `spec.md` §5 traces to a passing test
- [x] `make check` passes (ruff · mypy --strict · pytest · trace)
- [x] `make openapi` run and `docs/openapi.json` updated
- [x] Spec status moved to `Implemented`
- [x] Module docstrings explain the *why*
- [x] `docs/02-lld.md` §15 build status updated
