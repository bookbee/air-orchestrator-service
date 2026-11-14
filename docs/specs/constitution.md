# Constitution

The rules every spec in this repository is checked against. A spec that conflicts with an
article here is wrong by default — the spec changes, or the article is amended first and the
amendment is justified in the same pull request.

This file is deliberately short. It holds only what must never drift. Rationale lives in
[`docs/00-plan.md`](../00-plan.md); design lives in the HLD and LLD; testable behaviour lives
in the numbered specs beside this file.

**Version:** 1.1.0 · **Ratified:** 2026-09-12 · **Amended:** 2026-11-14 · **Amendment:** see
§Amendment below.

---

## Article I — The channel comes from the credential

The channel (`customer` / `business`) is resolved from the authenticated principal and from
nothing else. No header, query parameter, body field or environment variable may select it.
`/v1/chat` and `/v1/query` are separate handlers pinned by `require_customer` /
`require_business`, never one handler that branches.

Channel determines guardrail profile, output contract, audit sink, quota bucket and tool
allow-list. A spec that lets a caller influence any of those without a different credential
violates this article.

*Derives from:* G9 · HLD §3.

## Article II — Mutations are proposed, never performed

This service holds no mutating capability. A turn may return a `Proposal` and change nothing.
Execution requires a **second** turn carrying a structured `confirm.proposal_id`.

Prose is never consent. A model talked into agreeing cannot produce the structured field, so a
successful prompt injection must remain a dead end rather than a write. A stale, mismatched,
declined or replayed confirmation is **not an error** — the turn answers normally, so the
response can never be used to probe which proposal ids exist.

*Derives from:* G3, G4 · HLD §6 · Plan §4 Q3.

## Article III — A missing downstream narrows the answer; it never refuses one

Absence of a downstream removes a capability from a turn. It does not fail the turn, and it
does not stop the service from starting. Only two things may prevent startup: settings that do
not validate, and a key file that does not parse.

`degraded` and `blocked` are distinct and must stay distinct: a guardrail refusing is the
system working, a downstream being unreachable is the system coping. Collapsing them makes a
refusal spike indistinguishable from an outage.

*Derives from:* G5 · HLD §7.

## Article IV — Every key is tenant-namespaced

Tenant comes from the authenticated principal and is a component of every session key, cache
key and downstream call. Absent and not-yours are indistinguishable to the caller — both 404.

This holds in the in-process store exactly as it will in Redis, because the key shape is what
the durable backend inherits.

*Derives from:* G6 · Plan §4 "Cache isolation".

## Article V — A stage that cannot run says so

A pipeline stage this build cannot really execute reports `skipped` with a reason. It may
never report `ok`. `_STUBBED` in `engine/turn.py` is the single list, and removing an entry is
what "turning a stage on" means.

A stand-in must not appear to understand more than it does. This is why routing is a literal
`/propose` trigger rather than something that looks like intent detection.

*Derives from:* LLD §15.

## Article VI — Public contracts are closed sets

`constants.py` enums, `schemas/events.py` models and the RFC 9457 error shape are public
surface. They appear in API responses *and* Prometheus label values simultaneously. Renaming a
member is a breaking change for callers and dashboards alike, and requires a spec.

Clients must ignore unknown event names. That tolerance is what allows additive change without
a version bump, and it only works if existing names never change meaning.

*Derives from:* G2 · LLD §4.

## Article VII — All model traffic goes through air-model-gateway

No provider SDK and no provider key enters this repository. `air-model-gateway` on :8083 is
the only model path, reached directly over HTTP and never through air-infra or through another
AIR service. This service asks for a model **class** (`local`, `auto`, `cloud`), never a model
or provider name: which models serve a class is the gateway owner's decision, made in the
gateway's own routing file, so changing it never requires a change here.

Services that make their own model calls — air-classifier among them — reach the same gateway
on their own credential. This service calls *them* for their capability and never classifies,
retrieves or embeds by calling a model itself on their behalf.

*Derives from:* G7 · Plan §3 · Amended 1.1.0 (spec [0003](0003-model-gateway/spec.md)).

## Article VIII — Nothing is a module-level singleton

`create_app(settings)` takes its settings as an argument and never reads the environment.
Shared collaborators live on one frozen `AppState` reached through dependency injection, so a
test can assemble a complete application with bespoke settings and swap exactly one
collaborator.

A spec whose implementation requires mutating the environment or clearing a cache to be
testable is not yet ready.

*Derives from:* LLD §2 · CLAUDE.md.

## Article IX — Secrets never render

No raw API key or digest appears in a log line, a repr, an exception message or an API
response. Isolation keys are not exposed in session views.

## Article X — A claim is proved by a test, not by assertion

Every requirement in a spec carries a stable `REQ-` id and must name the test that proves it,
or be explicitly marked `GAP` or `DEFERRED` with a reason. `make trace` enforces the link and
fails on a reference to a test that does not exist.

An untested requirement is a claim, and this repository does not ship claims as facts.

---

## Amendment

Amending an article requires:

1. The change to this file, with the version bumped — MAJOR for removing or reversing an
   article, MINOR for adding one, PATCH for wording that does not change meaning.
2. A note in the pull request naming which specs were re-checked against the new text.
3. Any spec that the amendment invalidates updated in the same change.

Articles I, II and IV are load-bearing for safety. Weakening any of them is a platform
decision, not a repository one.

### Amendment log

| Version | Date | Article | Change | Specs re-checked |
| --- | --- | --- | --- | --- |
| 1.1.0 | 2026-11-14 | VII | The model path is air-model-gateway (air-llm was retired and its `/v1/inference` removed); callers choose a class, never a model; capability services make their own model calls. MINOR: the article's intent (one model path, over HTTP, no provider keys here) is unchanged, its named service and its model-choice rule are new. | 0001 (unaffected: no model call), 0002 (unaffected: sessions only), 0003, 0004, 0005 |
