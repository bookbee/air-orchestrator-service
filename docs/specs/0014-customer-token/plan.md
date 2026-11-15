# 0014 — Implementation plan

**Spec:** [`spec.md`](spec.md) · **Status:** Ready · **Updated:** 2026-11-14

> `Principal.subject` is added by spec 0012's T2. If this spec lands first, its T2 adds the
> field instead; whichever lands second finds it there.

## 1. Approach

**One verifier, one place it runs.** A new `security/customer_token.py` holds
`CustomerTokenVerifier`, built once in `create_app` from `security.customer_tokens` and kept
on `AppState`. `api/deps.require_principal`, which already resolves the API key for every
authenticated route, gains a second step after the key:

```
record = key_store.lookup(api_key)            # unchanged; 401 missing-api-key
principal = record.to_principal()
if principal.channel is CUSTOMER and header present:
    subject = verifier.verify(token, tenant=principal.tenant)   # raises → 401 invalid-customer-token
    principal = principal.model_copy(update={"subject": subject})
metrics.record_customer_token(outcome)        # absent | valid | <failure>
bind_request_context(..., subject_hash=…)     # only when a subject was set
```

The step runs in `require_principal`, not in the chat route, for one reason: the session
routes use `require_principal`, and `REQ-CTK-007` needs the subject there too. A business
principal never reaches the verification branch, which is `REQ-CTK-006` by construction.
Because it is a dependency, a refusal is raised before the handler runs, so before any SSE
byte. That gives `REQ-CTK-005` its "before any event" for free, on both response shapes.

**Verification order** (`REQ-CTK-002`, `003`). Cheap and structural checks come first, and
nothing is trusted before the signature except the fields used to choose the key:

1. Size at most 4096 bytes, three base64url segments; otherwise `malformed`.
2. Unverified header: `typ == "air-customer+jwt"`, and `kid` in the configured map;
   otherwise `malformed` / `unknown_key`.
3. Header `alg` must equal the key's pinned `alg`, else `bad_algorithm`. This happens before
   PyJWT is called, and PyJWT is then called with `algorithms=[key.alg]`, so a token's
   header can never pick the algorithm.
4. `jwt.decode(token, key.public_key, algorithms=[key.alg], audience="air-orchestrator-service",
   issuer=key.issuer, leeway=30, options={"require": ["exp", "iat", "sub", "iss", "aud"]})`.
   Its exceptions map to `bad_signature`, `expired`, `not_yet_valid`, `wrong_issuer` and
   `wrong_audience`.
5. Then this service's own checks: `exp − iat ≤ 900` (`too_long_lived`); `sub` pattern
   (`bad_subject`); `air_tenant` equal to the key's tenant and to the principal's tenant
   (`wrong_tenant`).

Every failure raises `CustomerTokenRejectedError(outcome)`. The dependency turns it into
`InvalidCustomerTokenError`, one fixed body, and logs `customer_token.rejected` with the
outcome and `key_id` only. The token string is never passed to a logger, an exception
message or a repr: the exception carries the outcome alone (`REQ-CTK-008`).

**Keys** (`REQ-CTK-010`). `security.customer_tokens` is a JSON list of `{issuer, tenant, kid,
alg, public_key}`. `public_key` is a PEM, or a JWK as a JSON object. A settings validator
loads each with `cryptography`. Startup is refused when:
- the key does not parse;
- it is a private key, or the PEM text contains `PRIVATE`;
- it is not P-256 for `ES256`;
- a `kid` is duplicated;
- an `alg` is outside `{"ES256"}`.

Two entries may share an issuer and tenant with different `kid`s, which is the rotation case
(§9 Q2). Nothing is fetched (`REQ-CTK-011`).

**Sessions** (`REQ-CTK-007`). `Session` gains `owner_subject: str | None`, set at `create()`
from the principal. `Session.owned_by(key_id, tenant)` becomes `owned_by(principal)`. It
compares key id, tenant **and** subject, with `None` equal only to `None`. The one call site
(`InMemorySessionStore.get`) passes the principal. `delete` goes through `get`, so it
inherits the check, and turns already go through `get`. A mismatch is the existing `None`,
which the routes already turn into the uniform 404. The store's key shape,
`(tenant, session_id)`, is unchanged, so spec 0002's Redis design is unaffected beyond one
more stored field.

## 2. Modules touched

| Path | Change | Notes |
| --- | --- | --- |
| `security/customer_token.py` | new | `CustomerTokenKey`, `CustomerTokenVerifier`, `CustomerTokenRejectedError`, the outcome set |
| `config.py` | modified | `SecuritySettings.customer_tokens: list[CustomerTokenKey]` with the startup validator |
| `api/deps.py` | modified | `require_principal` verifies on the customer channel; `AppState.customer_tokens`; `subject_hash` bound |
| `api/errors.py` | modified, additive | `InvalidCustomerTokenError` (`invalid-customer-token`, 401) in the §12 catalogue |
| `schemas/common.py` | modified, additive | `Principal.subject` (if spec 0012 T2 has not added it) |
| `schemas/session.py` | modified | `Session.owner_subject`; `owned_by(principal)` |
| `memory/session.py` | modified | `create` records the subject; `get` passes the principal |
| `observability/metrics.py` | modified | `record_customer_token(outcome)`; closed outcome set |
| `main.py` | modified | builds the verifier |
| `api/v1/chat.py`, `api/v1/sessions.py` | modified, docs | `air-token` declared via `openapi_extra` as an optional header, described as customer-channel only. It is read in `require_principal`, so it is documented on the routes where it matters, not on `/v1/query` |
| `pyproject.toml` | modified | runtime `pyjwt[crypto]>=2.10` (brings `cryptography`) |
| `.env.example` | modified | `SECURITY__CUSTOMER_TOKENS` with air-web-console's development **public** key (`kid` `brightcart-dev-1`, issuer `air-web-console-dev`, tenant `brightcart`) once that session reports it |
| `docs/02-lld.md` §3, §12; `docs/openapi.json` | modified | principal resolution, the new problem type |

**Not changed, though a reviewer might expect them to be:**

- **`engine/turn.py`.** The subject reaches the engine on the `Principal` it already
  receives. Nothing in the turn needs to know a token exists. That is the claim this design
  makes: identity is resolved at the edge, and the engine sees a principal.
- **`clients/tools_gateway.py`.** It already reads `principal.subject` (spec 0012,
  `REQ-TGW-008`).
- **`security/api_keys.py`.** The key record says nothing about customers. Mixing them would
  make one credential describe two parties.
- **`api/middleware.py`.** The access log already omits headers. The token is never in it.
- **The session store's key shape.** Ownership gains a field, but the key doesn't, so absent
  and not-yours stay indistinguishable at no extra cost.

## 3. Contract impact

- [ ] No change to public contracts
- [x] Additive only: an optional request header on `/v1/chat` and the session routes; a new
  problem type `invalid-customer-token` (401) in the existing RFC 9457 shape. `make openapi`.
- [ ] Breaking

**Behaviour change worth flagging.** Once this lands, an existing session started without a
token can't be continued *with* one, and the reverse. A tester switching air-web-console's
Customer picker mid-conversation gets a 404 and a new session. That is `REQ-CTK-007` working.
air-web-console should start a new session when the picker changes; its handoff should say so.

## 4. Configuration

| Setting | Default | Why it is configurable |
| --- | --- | --- |
| `SECURITY__CUSTOMER_TOKENS` | `[]` | Issuers and public keys per tenant. Empty means every presented token is `unknown_key` (401), while a request without one is served anonymously. A fresh checkout needs no keys |

Lifetime (900 s), leeway (30 s), the size limit (4096) and the audience
(`air-orchestrator-service`) are constants. Spec 0014 fixes them, and a setting would let a
deployment quietly weaken them.

## 5. Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Verify in the chat route | The session routes need the subject too (`REQ-CTK-007`). Two call sites is how one forgets |
| Let PyJWT read `alg` from the header with a list of allowed algorithms | The classic algorithm-confusion bug lives exactly there. The key pins it, and the header must match it |
| HS256 with a shared secret per tenant | The orchestrator would hold a secret that can mint tokens. With ES256 it holds only public keys, so a leak of its settings forges nothing |
| `Authorization: Bearer` for the token | The API key is the caller's credential and the token is the customer's. Two headers keep two parties apart, and leave `Authorization` free for a future gateway |
| Downgrade an invalid token to anonymous | Decided against (§9 Q3) |
| Store the subject on the session and trust it on later turns | `REQ-CTK-009`: a later turn must present its own token, or a stolen session id would carry someone's identity |
| `python-jose` or `authlib` | PyJWT is the most used, small, typed, and does exactly this; `cryptography` comes with it |

## 6. Test strategy

Hermetic. A fixture generates a P-256 key pair per test session with `cryptography`, and a
second, unrelated pair. A `mint(**overrides)` helper builds tokens with PyJWT, including
deliberately bad ones (an `HS256` token signed with the public key bytes, `alg: none`, a
re-encoded payload). No clock is mocked: `iat`/`exp` are set relative to `time.time()` with margins far
wider than the 30 s leeway, so the tests can't flake on timing.

| Requirement | Test | Approach |
| --- | --- | --- |
| `REQ-CTK-001` | `test_customer_token.py::test_a_valid_token_sets_the_subject` | A turn through `/v1/chat`; principal subject observed via a patched engine entry point |
| `REQ-CTK-002` | `…::test_each_failed_check_refuses_the_token` | Parametrised over every outcome in `REQ-CTK-012` |
| `REQ-CTK-003` | `…::test_the_algorithm_is_pinned_by_the_key` | AC-2 |
| `REQ-CTK-004` | `…::test_no_token_is_an_anonymous_customer` | Subject `None`, turn answers |
| `REQ-CTK-005` | `…::test_every_refusal_is_one_401_before_any_event` | JSON and SSE requests; bodies compared for byte equality across reasons; no `turn.start` |
| `REQ-CTK-006` | `…::test_the_business_channel_ignores_the_token` | A garbage token on `/v1/query` is still `200` |
| `REQ-CTK-007` | `test_session_store.py::test_a_session_is_invisible_to_another_subject` | Store level, then routes: turn and `GET`/`DELETE` |
| `REQ-CTK-008` | `…::test_the_token_never_appears_in_logs_or_responses` | `caplog` at DEBUG plus response bodies, searched for each segment |
| `REQ-CTK-009` | `…::test_the_subject_has_no_other_source` | A body `subject` field is a 422 (`extra="forbid"`); an `X-Subject` header and a `?subject=` parameter leave the subject `None` |
| `REQ-CTK-010` | `test_config.py::test_customer_token_keys_are_validated_at_startup` | Private PEM, P-384 key, duplicate kid, `RS256`, garbage |
| `REQ-CTK-011` | `…::test_verification_makes_no_network_call` | `respx.mock(assert_all_mocked=True)` with no routes: verification still succeeds |
| `REQ-CTK-012` | `…::test_every_outcome_is_counted` | Registry samples per outcome |

## 7. Rollout

- Additive and inert until keys are configured. With an empty key list nothing changes for a
  caller that sends no token, which today is every caller.
- **Order with air-web-console:** the console can ship first, because the orchestrator
  ignores the header until this lands. Then put the console's development public key in
  `.env.example` here and turn on the console's Customer picker.
- The session-binding change (§3) ships in the same task as the verifier, not later. Any
  window where subjects exist but sessions aren't bound to them is the leak the spec was
  written to close.
- Turning it off: remove the keys. Presented tokens then refuse (`unknown_key`). A client
  that stops sending them is served anonymously.

## 8. Risks

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| A deployment configures a private key by mistake | Low | Startup refuses it (`REQ-CTK-010`) |
| Clock skew between the minter and this service | Medium | 30 s leeway; per-request minting; `not_yet_valid` and `expired` counted separately so skew is visible |
| A token leaks through an error path | Low | It never enters an exception or log call; `REQ-CTK-008`'s test searches every captured line |
| Sessions drop when a tester switches customer | Certain, by design | Documented (§3); the console starts a new session on change |
| PyJWT's exception taxonomy changes | Low | Mapped in one function with a test per outcome |
