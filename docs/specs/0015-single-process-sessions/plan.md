# 0015 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Built · **Updated:** 2026-11-15

## 1. Approach

`__main__.main` asks one function, `config.effective_workers(settings)`, how many workers
to start. It returns `app.workers` when the session store is shared across processes and
1 otherwise, logging `app.workers_reduced` when that is less than configured. Today no
backend is shared (`redis` falls back to memory), so it is always 1; spec 0002 changes
the one predicate, `session_store_is_shared`, when Redis lands.

Reducing beats refusing (spec §3): a refusal turns a half-broken deployment into a crash loop.

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `src/air_orchestrator_service/config.py` | modified | `workers` default 1; `session_store_is_shared`, `effective_workers` |
| `src/air_orchestrator_service/__main__.py` | modified | `workers=effective_workers(settings)` |
| `.env.example` | modified | `APP__WORKERS=1` |
| `src/air_orchestrator_service/memory/session.py` | unchanged | The store is right; the process count was wrong |

`make run` uses uvicorn directly, with one process, and is unaffected.

## 3. Contract impact

- [x] No change to public contracts

## 4. Decisions taken

- Reduce with a warning, not refuse (spec §3).
- The predicate lives beside the session settings, so spec 0002 has one place to change.

## 5. Traceability

| Id | Test |
| --- | --- |
| `REQ-SPS-001` | `test_entrypoint.py::test_an_in_process_session_store_runs_one_worker` |
| `REQ-SPS-002` | `test_entrypoint.py::test_one_worker_is_the_default_and_the_example` |
