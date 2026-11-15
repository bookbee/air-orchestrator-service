"""The customer token — spec 0014.

The verifier first (T1): every check in ``REQ-CTK-002`` refuses with its own outcome,
the algorithm is the key's, nothing touches the network, and every outcome is counted.
Then principal resolution on the real app (T2): the turn's principal is observed through
``FixedPlanner.seen``, which records the context each turn was planned with.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI

from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import Route
from air_orchestrator_service.engine.plan import Plan
from air_orchestrator_service.observability import metrics
from air_orchestrator_service.security.customer_token import (
    MAX_TOKEN_BYTES,
    OUTCOMES,
    CustomerTokenRejectedError,
    CustomerTokenVerifier,
    subject_hash,
)
from tests.conftest import BUSINESS_KEY, CUSTOMER_KEY, FixedPlanner, auth, build_app, use_planner
from tests.unit._customer_token_support import (
    KID,
    SUBJECT,
    TENANT,
    configured_key,
    forged,
    mint,
    private_key,
)


@pytest.fixture
def verifier() -> CustomerTokenVerifier:
    return CustomerTokenVerifier([configured_key()])


def _outcome(verifier: CustomerTokenVerifier, token: str, tenant: str = TENANT) -> str:
    try:
        verifier.resolve(token, tenant=tenant)
    except CustomerTokenRejectedError as exc:
        return exc.outcome
    return "valid"


_NOW = int(time.time())

#: One token per failed check, by the outcome it must give (``REQ-CTK-002``).
_BAD: dict[str, Any] = {
    "garbage": ("not-a-token", "malformed"),
    "two segments": ("a.b", "malformed"),
    "oversized": (lambda: "a." * (MAX_TOKEN_BYTES // 2) + "b", "malformed"),
    "wrong typ": (lambda: mint(header={"typ": "JWT"}), "malformed"),
    "no sub": (lambda: mint(sub=None), "malformed"),
    "unknown kid": (lambda: mint(header={"kid": "someone-else"}), "unknown_key"),
    "no kid": (lambda: mint(header={"kid": None}), "unknown_key"),
    "hs256": (lambda: forged("HS256"), "bad_algorithm"),
    "none": (lambda: forged("none"), "bad_algorithm"),
    "another key": (lambda: mint(signer=private_key("stranger")), "bad_signature"),
    "expired": (lambda: mint(iat=_NOW - 1200, exp=_NOW - 600), "expired"),
    "issued in the future": (lambda: mint(iat=_NOW + 600, exp=_NOW + 900), "not_yet_valid"),
    "lives too long": (lambda: mint(exp=_NOW + 3600), "too_long_lived"),
    "wrong issuer": (lambda: mint(iss="someone-else"), "wrong_issuer"),
    "wrong audience": (lambda: mint(aud="air-rag-service"), "wrong_audience"),
    "wrong token tenant": (lambda: mint(air_tenant="othershop"), "wrong_tenant"),
    "no token tenant": (lambda: mint(air_tenant=None), "wrong_tenant"),
    "bad subject": (lambda: mint(sub="cust 1001; drop"), "bad_subject"),
}


@pytest.mark.parametrize("case", list(_BAD))
def test_each_failed_check_refuses_the_token(verifier: CustomerTokenVerifier, case: str) -> None:
    """``REQ-CTK-002``. A good token verifies to its ``sub``; each failed check refuses
    with its own outcome. The audience may be a list containing ours."""
    token, expected = _BAD[case]
    assert _outcome(verifier, token() if callable(token) else token) == expected
    assert verifier.resolve(mint(), tenant=TENANT) == SUBJECT
    assert (
        verifier.resolve(mint(aud=["other", "air-orchestrator-service"]), tenant=TENANT) == SUBJECT
    )


def test_a_token_for_another_api_key_tenant_is_refused(verifier: CustomerTokenVerifier) -> None:
    """``REQ-CTK-002``: ``air_tenant`` must equal the key's tenant *and* the API key's."""
    assert _outcome(verifier, mint(), tenant="othershop") == "wrong_tenant"


def test_the_algorithm_is_pinned_by_the_key(verifier: CustomerTokenVerifier) -> None:
    """``REQ-CTK-003``, AC-2. ``HS256`` keyed with the public key's own bytes, and
    ``alg: none``, are both ``bad_algorithm``: the header can only agree with the
    key's algorithm, never choose one."""
    assert _outcome(verifier, forged("HS256")) == "bad_algorithm"
    assert _outcome(verifier, forged("none")) == "bad_algorithm"


def test_verification_makes_no_network_call(verifier: CustomerTokenVerifier) -> None:
    """``REQ-CTK-011``. Any request at all would fail ``assert_all_mocked``."""
    with respx.mock(assert_all_mocked=True, assert_all_called=False):
        assert verifier.resolve(mint(), tenant=TENANT) == SUBJECT
        assert _outcome(verifier, mint(header={"kid": "someone-else"})) == "unknown_key"


def _count(outcome: str) -> float:
    value = metrics.REGISTRY.get_sample_value(
        "air_orchestrator_service_customer_tokens_total", {"outcome": outcome}
    )
    return value or 0.0


def test_every_outcome_is_counted(verifier: CustomerTokenVerifier) -> None:
    """``REQ-CTK-012``, at the verifier: ``absent``, ``valid`` and each failure once."""
    tokens: dict[str, str | None] = {"absent": None, "valid": mint()}
    for token, outcome in _BAD.values():
        tokens.setdefault(outcome, token() if callable(token) else token)
    assert set(tokens) == set(OUTCOMES)
    before = {outcome: _count(outcome) for outcome in OUTCOMES}

    for token in tokens.values():
        with contextlib.suppress(CustomerTokenRejectedError):
            verifier.resolve(token, tenant=TENANT)

    assert {o: _count(o) - before[o] for o in OUTCOMES} == dict.fromkeys(OUTCOMES, 1.0)


def test_the_rejection_carries_nothing_from_the_token(verifier: CustomerTokenVerifier) -> None:
    """``REQ-CTK-008``, at the verifier: the exception and the verifier's repr name no
    part of the token."""
    token = mint(iss="someone-else")
    with pytest.raises(CustomerTokenRejectedError) as refused:
        verifier.resolve(token, tenant=TENANT)
    for segment in token.split("."):
        assert segment not in str(refused.value) and segment not in repr(refused.value)
        assert segment not in repr(verifier)
    assert verifier.kid_of(token) == KID


# ── T2: principal resolution and the 401 ─────────────────────────────────────


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    security = settings.security.model_copy(update={"customer_tokens": [configured_key()]})
    return build_app(settings.model_copy(update={"security": security}))


async def _post(
    app: FastAPI,
    *,
    token: str | None = None,
    path: str = "/v1/chat",
    key: str = CUSTOMER_KEY,
    stream: bool = False,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> tuple[httpx.Response, FixedPlanner]:
    planner = FixedPlanner(Plan(routes=(Route.DIRECT,), reason="direct answer"))
    use_planner(app, planner)
    sent = {**auth(key), **(headers or {})}
    if token is not None:
        sent["air-token"] = token
    if stream:
        sent["Accept"] = "text/event-stream"
    field = "message" if path == "/v1/chat" else "query"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            path, headers=sent, params=params, json=body or {field: "Where is my order?"}
        )
    return response, planner


def _subject(planner: FixedPlanner) -> str | None:
    [seen] = planner.seen
    return seen.principal.subject


async def test_a_valid_token_sets_the_subject(app: FastAPI) -> None:
    """``REQ-CTK-001``, AC-1. The header name is case-insensitive."""
    response, planner = await _post(app, token=mint())
    assert response.status_code == httpx.codes.OK, response.text
    assert _subject(planner) == SUBJECT

    response, planner = await _post(app, headers={"Air-Token": mint()})
    assert response.status_code == httpx.codes.OK, response.text
    assert _subject(planner) == SUBJECT


async def test_no_token_is_an_anonymous_customer(app: FastAPI) -> None:
    """``REQ-CTK-004``, AC-4. No header: the turn answers, with no subject."""
    response, planner = await _post(app)

    assert response.status_code == httpx.codes.OK, response.text
    assert response.json()["answer"]
    assert _subject(planner) is None


_REFUSED = {
    "garbage": lambda: "not-a-token",
    "expired": lambda: mint(iat=_NOW - 1200, exp=_NOW - 600),
    "wrong tenant": lambda: mint(air_tenant="othershop"),
    "hs256": lambda: forged("HS256"),
    "another key": lambda: mint(signer=private_key("stranger")),
}


def _comparable(response: httpx.Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    return {k: v for k, v in body.items() if k not in ("request_id", "instance")}


async def test_every_refusal_is_one_401_before_any_event(app: FastAPI) -> None:
    """``REQ-CTK-005``, AC-3. Every reason, JSON or SSE, is the same 401 problem, and
    an SSE caller gets the problem instead of a stream: no ``turn.start``."""
    bodies = []
    for make in _REFUSED.values():
        for stream in (False, True):
            response, planner = await _post(app, token=make(), stream=stream)
            assert response.status_code == httpx.codes.UNAUTHORIZED, response.text
            assert response.headers["content-type"].startswith("application/problem+json")
            assert "turn.start" not in response.text
            assert planner.seen == []
            bodies.append(_comparable(response))
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]["type"].endswith("invalid-customer-token")


async def test_the_business_channel_ignores_the_token(app: FastAPI) -> None:
    """``REQ-CTK-006``. Garbage on ``/v1/query`` is not verified, not counted, and the
    turn is answered as if it were absent."""
    before = {outcome: _count(outcome) for outcome in OUTCOMES}

    response, planner = await _post(app, path="/v1/query", key=BUSINESS_KEY, token="garbage")

    assert response.status_code == httpx.codes.OK, response.text
    assert _subject(planner) is None
    assert {outcome: _count(outcome) for outcome in OUTCOMES} == before


async def test_the_token_never_appears_in_logs_or_responses(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """``REQ-CTK-008``, AC-6. With debug logging, no segment of any token sent, good
    or bad, is in a log line, a response body or a response header. The subject is
    logged only as its hash."""
    tokens = [make() for make in _REFUSED.values()] + [mint()]
    responses = []
    with caplog.at_level(logging.DEBUG):
        for token in tokens:
            response, _ = await _post(app, token=token)
            responses.append(response)

    rendered = [caplog.text] + [r.text + str(r.headers) for r in responses]
    for token in tokens:
        for segment in (s for s in token.split(".") if len(s) > 8):
            assert all(segment not in text for text in rendered)
    assert SUBJECT not in caplog.text
    assert subject_hash(SUBJECT) in caplog.text
    assert caplog.text.count("customer_token.rejected") == len(_REFUSED)


async def test_the_subject_has_no_other_source(app: FastAPI) -> None:
    """``REQ-CTK-009``. A ``subject`` body field is refused by the closed request
    model; an ``X-Subject`` header and a ``?subject=`` parameter set nothing."""
    body, _ = await _post(app, body={"message": "hi", "subject": "cust_9"})
    assert body.status_code == httpx.codes.UNPROCESSABLE_ENTITY

    header, planner = await _post(app, headers={"X-Subject": "cust_9"})
    assert header.status_code == httpx.codes.OK and _subject(planner) is None

    query, planner = await _post(app, params={"subject": "cust_9"})
    assert query.status_code == httpx.codes.OK and _subject(planner) is None
