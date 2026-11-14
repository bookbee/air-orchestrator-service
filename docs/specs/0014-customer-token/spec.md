# 0014 — The customer token: who the customer is

**Status:** Accepted · **Area:** `CTK` · **Phase:** 3 · **Updated:** 2026-11-14
**Derives from:** spec [0012](../0012-tools-gateway/spec.md) §9 Q1 (decided) · [Plan §4 "Customer identity"](../../00-plan.md) ·
[HLD §3](../../01-hld.md) · constitution Articles I, IV, IX
**Supersedes:** —

## 1. Problem

A customer-channel API key identifies an application and a tenant (`brightcart`), not a
shopper. Every customer of that storefront arrives under the same key, so this service cannot
tell whose orders "my orders" means. Spec 0012 already withholds every per-customer
capability from a turn with no verified subject (`REQ-TGW-008`), so today none of them is
reachable. A second gap follows from the same fact. Sessions are owned by `(tenant, key_id)`
(`memory/session.py`), so two shoppers behind one storefront key could read and continue each
other's conversation if one learned the other's session id. Once turns contain order details,
that is a data leak.

## 2. Outcome

A storefront that has signed a shopper in sends, beside its API key, a short-lived signed
**customer token** saying which customer this is. This service verifies it locally, with no
network call, against public keys configured per tenant. The token's subject becomes the
principal's `subject`, and through it the `subject` air-tools-gateway enforces ownership with.
A request without a token is an anonymous customer: it is answered, and per-customer
capabilities are simply not offered. A request with a token that fails any check is refused
before the turn starts, with one uniform answer. A session belongs to the subject that
started it, and is invisible to anyone else.

## 3. Scope

**In scope**

- The token's format, the header that carries it, and every check that makes it valid.
- How a verified token sets `Principal.subject`, and what happens without one.
- Binding sessions to the subject.
- Key configuration (public keys only), logging, metrics.

**Out of scope**

- **Minting tokens.** That is the storefront's job, or air-web-console's in development. The
  format here is their contract.
- **Signing customers in.** Passwords, OTP, social login: the storefront's.
- **Revocation lists and `jti` replay tracking.** The token lives for minutes. See §9 Q1.
- **Fetching keys from a JWKS URL.** v1 keys are configured. Rotation is a settings change
  with both keys listed (§9 Q2).
- **The business channel.** Analysts are not customers. A token there is ignored
  (`REQ-CTK-006`).

## 4. Constitution check

| Article | Relevance | Compliance |
| --- | --- | --- |
| I — The channel comes from the credential | A second credential arrives | The channel still comes from the API key alone. The token adds a subject on the customer channel only and changes no profile, quota or allow-list (`REQ-CTK-006`) |
| II — Mutations are proposed, never performed | A subject makes per-customer writes reachable | Unchanged: the subject is what a proposal is bound to (`REQ-TGW-019`); confirmation still needs the structured field |
| III — A missing downstream narrows | No downstream involved | Verification is local (`REQ-CTK-011`), so no service's absence can affect it |
| IV — Every key is tenant-namespaced | The token names a tenant | It must equal the API key's tenant (`REQ-CTK-002`); sessions gain the subject in their ownership, and not-yours is still 404 (`REQ-CTK-007`) |
| VI — Public contracts are closed sets | A new header and a new problem type | Both additive. The header is optional on `/v1/chat`; the problem is a new `type` in the existing RFC 9457 shape |
| VIII — No module-level singletons | A verifier with keys | Built from settings in `create_app`, on `AppState` |
| IX — Secrets never render | The token is a bearer credential | It never appears in a log, repr, error or response; the subject is logged as a hash prefix only (`REQ-CTK-008`) |
| X — Proved by a test | Forward spec | Every row names its test, `DEFERRED` until built |

## 5. Requirements

**The token**

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-CTK-001` | MUST | On `POST /v1/chat` (and the session routes, `REQ-CTK-007`), an `air-token` header carrying a token that passes `REQ-CTK-002` sets `Principal.subject` to the token's `sub`. The name matches the header existing AIR applications outside this repository already use; HTTP header names are case-insensitive, so `Air-Token` is the same header. |
| `REQ-CTK-002` | MUST | A token is valid only if every one of these holds: it is a compact JWS (RFC 7515) of a JWT (RFC 7519); its header has `typ` `air-customer+jwt` and a `kid` naming a configured key; `alg` equals that key's configured algorithm; the signature verifies; `iss` equals the key's issuer; `aud` is or contains `air-orchestrator-service`; `exp` is in the future and `iat` not in the future, each within 30 s of leeway; `exp − iat` is at most 900 s; `sub` matches `^[A-Za-z0-9_.:-]{1,128}$`; and `air_tenant` equals both the key's tenant and the API key's tenant. |
| `REQ-CTK-003` | MUST NOT | A token is accepted with `alg` `none`, with any HMAC algorithm, or with any algorithm other than the one pinned on its key. v1 configures `ES256` only. |
| `REQ-CTK-004` | MUST | A customer-channel request without the header is served as an anonymous customer: `subject` is `None`, the turn runs, and per-customer capabilities are not offered (`REQ-TGW-008`). |
| `REQ-CTK-005` | MUST | A request whose header is present but fails any check in `REQ-CTK-002` or `003` is refused before any turn event with `401`, problem type `invalid-customer-token`, and one body for every reason. The reason is logged and counted, never returned. |
| `REQ-CTK-006` | MUST | On `POST /v1/query` the header is ignored: it is not verified, sets no subject, and does not change the response. |

**Sessions**

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-CTK-007` | MUST | A session records the subject (or its absence) that created it. A turn, `GET` or `DELETE` on that session from a principal with a different subject, including none where one was set, or one where none was, gets the same `404` as a session that does not exist. |

**Handling**

| Id | Level | Requirement |
| --- | --- | --- |
| `REQ-CTK-008` | MUST NOT | The token, or any segment of it, appears in a log line, repr, exception message, metric label or response. The subject appears in logs only as the first 12 hex characters of its sha256. |
| `REQ-CTK-009` | MUST NOT | `Principal.subject` is set from anything but a verified token: not a body field, another header, a query parameter, a session's stored value, or model output. |
| `REQ-CTK-010` | MUST | Verification keys are configured per issuer as `{issuer, tenant, kid, alg, public_key}`, the key as a PEM or JWK public key. Startup fails on a malformed key, a duplicate `kid`, an algorithm outside the allowed set, or a private key supplied where a public one belongs. |
| `REQ-CTK-011` | MUST NOT | Verification makes a network call. |
| `REQ-CTK-012` | MUST | Every customer-channel request is counted in `air_orchestrator_service_customer_tokens_total{outcome}`, outcome one of `absent`, `valid`, `malformed`, `unknown_key`, `bad_algorithm`, `bad_signature`, `expired`, `not_yet_valid`, `too_long_lived`, `wrong_issuer`, `wrong_audience`, `wrong_tenant`, `bad_subject`. |

## 6. Acceptance criteria

**AC-1 — A good token names the customer** (`REQ-CTK-001`, `002`)
- **Given** a brightcart customer key and a token from brightcart's configured key, with `sub` `cust_1001`, a 10-minute life, and `air_tenant` `brightcart`
- **When** `POST /v1/chat` is sent with both headers
- **Then** the turn's principal has subject `cust_1001`, and air-tools-gateway requests carry `"subject": "cust_1001"`

**AC-2 — The algorithm is the key's, never the token's** (`REQ-CTK-003`)
- **Given** a token re-signed with `HS256`, using brightcart's public key bytes as the HMAC secret, and a token with `alg: none`
- **When** each is sent
- **Then** both get `401 invalid-customer-token`, counted as `bad_algorithm`

**AC-3 — One tenant's token is useless on another's key** (`REQ-CTK-002`)
- **Given** a valid brightcart token
- **When** it is sent with an `othershop` customer key
- **Then** `401`, counted as `wrong_tenant`, and the body is identical to an expired token's

**AC-4 — No token is not an error** (`REQ-CTK-004`)
- **Given** a customer key and no token
- **When** a customer asks "where is my order?"
- **Then** the turn answers, no per-customer capability is offered, and no gateway call carries a subject

**AC-5 — A conversation belongs to its customer** (`REQ-CTK-007`)
- **Given** a session started by `cust_1001`
- **When** the same key with a token for `cust_1002`, or with no token, sends a turn or `GET /v1/sessions/{id}` for it
- **Then** both get the `404` a nonexistent session gets

**AC-6 — The token never echoes** (`REQ-CTK-008`)
- **Given** any of the failing tokens above, and a valid one
- **When** the requests run with debug logging
- **Then** no captured log line, response body or header contains any of the token's three segments

## 7. Failure and degradation

| Condition | Behaviour | Turn status |
| --- | --- | --- |
| No token | Anonymous customer; per-customer capabilities not offered | `ok` |
| Token invalid, expired, wrong tenant, unknown key | `401 invalid-customer-token` before any event; the client should re-mint and retry | — (no turn) |
| No verification keys configured | Every token is `unknown_key`, so `401`; a request without one is still served | `ok` without a token |
| Token on the business channel | Ignored | unchanged |

Verification is local, so no downstream outage can affect it.

## 8. Observability

- `air_orchestrator_service_customer_tokens_total{outcome}` (`REQ-CTK-012`).
- One `customer_token.rejected` log line per refusal, with `outcome`, `kid` (if parseable and
  configured) and `key_id`. Never the token.
- `subject_hash` (12 hex) bound on the logging context of a turn with a subject, beside
  `tenant` and `key_id`, so a customer's turns can be correlated without logging who they are.

## 9. Open questions

| # | Question | Blocks | Owner |
| --- | --- | --- | --- |
| Q1 | ~~Track `jti` against replay?~~ **Decided 2026-11-14** — see below. | — | Product owner |
| Q2 | ~~Configured keys or a JWKS URL?~~ **Decided 2026-11-14** — see below. | — | Product owner |
| Q3 | ~~`401` or anonymous on an invalid token?~~ **Decided 2026-11-14** — see below. | — | Product owner |
| Q4 | ~~Is 15 minutes the right maximum life?~~ **Decided 2026-11-14** — see below. | — | Product owner |

### Decisions taken

**Q1 — No `jti` tracking in v1.** A token lives at most 15 minutes, travels over TLS, and
is minted per request by a server-side BFF, so a replay window needs a stolen token and buys
at most that window. Tracking needs a store shared across replicas, which arrives with Redis
(spec 0002). Revisit then. `jti` stays in the format, so turning it on later is not a
format change.

**Q2 — Configured public keys in v1, two `kid`s per issuer during rotation.** Keys are
settings (`REQ-CTK-010`), and nothing is fetched (`REQ-CTK-011`). A rotation lists the old
and new key under the same issuer until the old one's tokens have all expired (15 minutes),
then removes it. A JWKS URL is a later spec, if a storefront needs to rotate without this
service's help.

**Q3 — An invalid token is `401`, never a silent downgrade** (`REQ-CTK-005`). A client
whose minting is broken learns it at once. Downgrading would turn "your token is wrong" into
"I can't see your orders", which reads like an outage.

**Q4 — 15 minutes maximum.** A per-request minter normally uses 10 (air-web-console's
default). The ceiling leaves room for clock skew and slow clients without making a stolen
token long-lived.

## 10. Traceability

> Forward spec: every row is `DEFERRED` until the tasks land.

| Id | Proof |
| --- | --- |
| `REQ-CTK-001` | DEFERRED — Phase 3 · `tests/unit/test_customer_token.py::test_a_valid_token_sets_the_subject` |
| `REQ-CTK-002` | DEFERRED — Phase 3 · `tests/unit/test_customer_token.py::test_each_failed_check_refuses_the_token` |
| `REQ-CTK-003` | DEFERRED — Phase 3 · `tests/unit/test_customer_token.py::test_the_algorithm_is_pinned_by_the_key` |
| `REQ-CTK-004` | DEFERRED — Phase 3 · `tests/unit/test_customer_token.py::test_no_token_is_an_anonymous_customer` |
| `REQ-CTK-005` | DEFERRED — Phase 3 · `tests/unit/test_customer_token.py::test_every_refusal_is_one_401_before_any_event` |
| `REQ-CTK-006` | DEFERRED — Phase 3 · `tests/unit/test_customer_token.py::test_the_business_channel_ignores_the_token` |
| `REQ-CTK-007` | DEFERRED — Phase 3 · `tests/unit/test_session_store.py::test_a_session_is_invisible_to_another_subject` |
| `REQ-CTK-008` | DEFERRED — Phase 3 · `tests/unit/test_customer_token.py::test_the_token_never_appears_in_logs_or_responses` |
| `REQ-CTK-009` | DEFERRED — Phase 3 · `tests/unit/test_customer_token.py::test_the_subject_has_no_other_source` |
| `REQ-CTK-010` | DEFERRED — Phase 3 · `tests/unit/test_config.py::test_customer_token_keys_are_validated_at_startup` |
| `REQ-CTK-011` | DEFERRED — Phase 3 · `tests/unit/test_customer_token.py::test_verification_makes_no_network_call` |
| `REQ-CTK-012` | DEFERRED — Phase 3 · `tests/unit/test_customer_token.py::test_every_outcome_is_counted` |
