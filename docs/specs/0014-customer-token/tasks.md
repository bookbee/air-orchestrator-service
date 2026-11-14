# 0014 — Tasks

**Spec:** [`spec.md`](spec.md) · **Plan:** [`plan.md`](plan.md) · **Updated:** 2026-11-14

> Each task leaves the tree green (`make check`). When one lands, its rows in spec §10 move
> from `DEFERRED` to the named test.

## Order

T1 → T2 → T3 → T4 → T5.

- **T2 and T3 land together, or T3 first.** A subject on the principal without sessions bound
  to it is the leak the spec exists to close (plan §7). Opening T2 as its own PR is fine only
  if T3 merges in the same release.
- **T4 needs air-web-console's public key**, reported by its session.
- Independent of spec 0012 except for `Principal.subject`, which whichever lands first adds.

## Tasks

### T1 — The verifier and its keys

- **Advances:** `REQ-CTK-002`, `003`, `010`, `011`, `012`
- **Touches:**
  - `security/customer_token.py` (new);
  - `config.py` (`SecuritySettings.customer_tokens` and its validator);
  - `observability/metrics.py` (`record_customer_token`);
  - `pyproject.toml` (`pyjwt[crypto]`).
- **Adds test:**
  - in `tests/unit/test_customer_token.py`:
    - `test_each_failed_check_refuses_the_token`;
    - `test_the_algorithm_is_pinned_by_the_key`;
    - `test_verification_makes_no_network_call`;
    - `test_every_outcome_is_counted`, at the verifier level;
  - `tests/unit/test_config.py::test_customer_token_keys_are_validated_at_startup`.
- **Done when:**
  - a good token verifies to its `sub`;
  - each bad token in the test table is refused with its own outcome;
  - a private key or a duplicate `kid` stops startup.

### T2 — Principal resolution and the 401

- **Advances:** `REQ-CTK-001`, `004`, `005`, `006`, `008`, `009`
- **Touches:**
  - `api/deps.py` (`require_principal`, `AppState.customer_tokens`, `subject_hash`);
  - `api/errors.py` (`InvalidCustomerTokenError`);
  - `schemas/common.py` (`Principal.subject`, if not already there);
  - `main.py`;
  - `api/v1/chat.py` and `api/v1/sessions.py` (`openapi_extra` header doc);
  - `docs/openapi.json`.
- **Adds test:** in `tests/unit/test_customer_token.py`:
  - `test_a_valid_token_sets_the_subject`;
  - `test_no_token_is_an_anonymous_customer`;
  - `test_every_refusal_is_one_401_before_any_event`;
  - `test_the_business_channel_ignores_the_token`;
  - `test_the_token_never_appears_in_logs_or_responses`;
  - `test_the_subject_has_no_other_source`.
- **Done when:**
  - `/v1/chat` with a good token has subject `cust_1001`;
  - with a bad one, JSON and SSE both get the identical 401 and no `turn.start`;
  - with none, it answers;
  - `/v1/query` ignores the header.

### T3 — Sessions belong to their customer

- **Advances:** `REQ-CTK-007`
- **Touches:** `schemas/session.py` (`owner_subject`, `owned_by(principal)`);
  `memory/session.py` (`create`, `get`).
- **Adds test:** `tests/unit/test_session_store.py::test_a_session_is_invisible_to_another_subject`,
  at store level and through `POST /v1/chat`, `GET` and `DELETE /v1/sessions/{id}`.
- **Done when:**
  - a session made by `cust_1001` is a 404 to `cust_1002` and to an anonymous caller on the
    same key, and the reverse;
  - every existing session test passes unchanged.

### T4 — Development key and joint check with air-web-console

- **Advances:** none new
- **Touches:** `.env.example` (`SECURITY__CUSTOMER_TOKENS` with the console's development
  public key); `README.md` (chatting as a customer).
- **Done when:** with both running, the console's chat as `cust_1001` reaches the engine with
  that subject (`subject_hash` on the turn's log lines), and the console's `expired`,
  `wrong tenant` and `tampered` modes each get the same `401 invalid-customer-token`.

### T5 — Close-out

- **Advances:** all
- **Touches:**
  - `docs/02-lld.md` §3 (principal resolution), §12 (problem catalogue), §15;
  - CLAUDE.md, under Architecture: the customer token and session ownership;
  - spec 0014 §10, and its status.
- **Done when:** `make check` and `make trace` pass, with every spec 0014 row naming a
  passing test.

## Definition of done

- [ ] Every requirement in `spec.md` §5 traces to a passing test, or is a declared `GAP`
- [ ] `make check` passes
- [ ] `make trace` passes
- [ ] `make openapi` run (header doc, problem type)
- [ ] Spec status moved to `Implemented` (or `Partial`, with gaps listed)
- [ ] `security/customer_token.py`'s docstring explains the why: pinned algorithm, public keys only, uniform refusal
- [ ] `docs/02-lld.md` §15 build status updated
