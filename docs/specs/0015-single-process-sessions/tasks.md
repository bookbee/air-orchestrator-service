# 0015 — Tasks

**Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-15

### T1 — One worker while sessions are in memory

- `effective_workers` and `session_store_is_shared` in `config.py`; `__main__` uses it.
- `app.workers` default and `.env.example` to 1.
- **Done when:** both spec rows name passing tests; `make check` and `make trace` pass.

## Status (2026-11-15)

T1 done. Found by air-web-console's SIT (OR-012). Spec 0002 must revisit
`session_store_is_shared` when the Redis backend is built, and replicas without session
affinity remain its problem (spec §3).

## Definition of done

- [x] Every requirement traces to a passing test
- [x] `make check` passes
- [x] `make trace` passes
