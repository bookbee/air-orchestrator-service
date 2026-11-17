# 0016 — Tasks

**Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-17

### T1 — Pin the service's side

- `tests/unit/test_front_door.py`: the route sets, proxy headers, no preflight.
- **Done when:** the seven **S** rows name passing tests; `make check` passes.

### T2 — Build the front door (air-infrastructure-service)

- Gateway, route, health check and backend policy in the shared chart, on for the
  orchestrator only; the network policy admits the load balancer.
- Terraform for the proxy-only subnet, the address and the rate rule.
- Render assertions in `make gke-validate`; `smoke-front-door.sh` for UAT.
- **Done when:** `make gke-validate` passes with the assertions for `REQ-FDR-002`, `007`,
  `010` and `011`.

### T3 — Prove it in UAT

- Deploy, then run the front-door smoke test: AC-4 (a stream arrives progressively),
  AC-5 (an internal path is a 404), AC-6 (the rate holds).
- Record the result here and move the six deferred rows' status in the spec.
- **Done when:** the three acceptance criteria pass against the UAT address.

## Status (2026-11-17)

T1 and T2 done. T3 waits for a UAT project: an internal load balancer cannot be created
on a laptop cluster, so the front door's six requirements are implemented and
render-checked but not yet observed working. Open questions 1 and 2 (the rate, the
hostname) are for the first UAT deployment.

## Definition of done

- [x] Every **S** requirement traces to a passing test
- [x] `make check` passes
- [x] `make trace` passes (six rows deferred, declared)
- [ ] T3: the front door's acceptance criteria pass in UAT
