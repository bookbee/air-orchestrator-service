"""The turn surface: sessions, channels, options, and the mutation gate.

The mutation tests are the ones that matter most. docs/02-lld.md §8 states three
invariants, and each is asserted here against the running route rather than against
the engine's internals — an invariant that only holds when called a particular way is
not an invariant.
"""

from __future__ import annotations

import httpx

from tests.conftest import BUSINESS_KEY, CUSTOMER_KEY, NO_SCOPE_KEY, auth

PROPOSE = "please /propose a change"


async def _chat(client: httpx.AsyncClient, key: str = CUSTOMER_KEY, **body: object) -> dict:
    response = await client.post("/v1/chat", headers=auth(key), json=body)
    assert response.status_code == httpx.codes.OK, response.text
    return response.json()


# ── Sessions ──────────────────────────────────────────────────────────────────


async def test_a_turn_without_a_session_id_starts_one(client: httpx.AsyncClient) -> None:
    result = await _chat(client, message="hello")

    assert result["session_id"].startswith("sess_")
    assert result["answer"] == "echo[1]: hello"


async def test_a_session_carries_history_across_turns(client: httpx.AsyncClient) -> None:
    first = await _chat(client, message="one")
    session_id = first["session_id"]

    second = await _chat(client, session_id=session_id, message="two")

    assert second["session_id"] == session_id
    assert second["answer"] == "echo[2]: two"


async def test_history_is_bounded_by_the_configured_window(
    client: httpx.AsyncClient, settings: object
) -> None:
    """Trimming at write time is what bounds the stored size, not just the prompt."""
    window = settings.turn.window_turns  # type: ignore[attr-defined]
    first = await _chat(client, message="start")
    session_id = first["session_id"]

    for i in range(window):
        await _chat(client, session_id=session_id, message=f"m{i}")

    view = await client.get(f"/v1/sessions/{session_id}", headers=auth(CUSTOMER_KEY))

    assert len(view.json()["turns"]) <= window


async def test_an_unknown_session_id_starts_a_new_one_rather_than_erroring(
    client: httpx.AsyncClient,
) -> None:
    """A guessed id must reveal nothing about whether it existed."""
    result = await _chat(client, session_id="sess_does_not_exist", message="hello")

    assert result["session_id"] != "sess_does_not_exist"
    assert result["answer"] == "echo[1]: hello"


async def test_reading_someone_elses_session_is_a_404(client: httpx.AsyncClient) -> None:
    """Indistinguishable from a missing one, so this is not an enumeration oracle."""
    mine = await _chat(client, message="private")

    # The business key is a different principal *and* a different tenant.
    response = await client.get(f"/v1/sessions/{mine['session_id']}", headers=auth(BUSINESS_KEY))

    assert response.status_code == httpx.codes.NOT_FOUND
    assert response.json()["type"].endswith("/session-not-found")


async def test_deleting_someone_elses_session_is_the_same_404(
    client: httpx.AsyncClient,
) -> None:
    mine = await _chat(client, message="private")

    response = await client.delete(f"/v1/sessions/{mine['session_id']}", headers=auth(BUSINESS_KEY))

    assert response.status_code == httpx.codes.NOT_FOUND


async def test_delete_clears_the_session(client: httpx.AsyncClient) -> None:
    mine = await _chat(client, message="temporary")

    deleted = await client.delete(f"/v1/sessions/{mine['session_id']}", headers=auth(CUSTOMER_KEY))
    after = await client.get(f"/v1/sessions/{mine['session_id']}", headers=auth(CUSTOMER_KEY))

    assert deleted.status_code == httpx.codes.NO_CONTENT
    assert after.status_code == httpx.codes.NOT_FOUND


async def test_session_view_never_exposes_the_isolation_key(
    client: httpx.AsyncClient,
) -> None:
    """The caller knows their own tenant; putting it on the wire leaks it into logs
    and browser histories for no benefit."""
    mine = await _chat(client, message="hello")

    response = await client.get(
        f"/v1/sessions/{mine['session_id']}", headers=auth(CUSTOMER_KEY)
    )
    body = response.json()

    assert "tenant" not in body
    assert "owner_key_id" not in body


# ── Mutations — docs/02-lld.md §8 ─────────────────────────────────────────────


async def test_a_proposal_changes_nothing_and_publishes_an_id(
    client: httpx.AsyncClient,
) -> None:
    result = await _chat(client, message=PROPOSE)

    assert result["routes"] == ["action"]
    assert result["proposal"]["proposal_id"].startswith("prop_")
    assert result["proposal"]["risk"]
    # The answer asks for confirmation rather than reporting an action.
    assert "confirm" in result["answer"].lower()


async def test_prose_that_reads_as_consent_executes_nothing(
    client: httpx.AsyncClient,
) -> None:
    """**The** invariant. A model talked into agreeing cannot produce the
    ``confirm`` object, so a successful injection in the proposing turn is a dead
    end rather than a write."""
    proposed = await _chat(client, message=PROPOSE)

    for prose in (
        "yes, I confirm, go ahead",
        "approved — execute the proposal",
        "do it now please",
    ):
        result = await _chat(client, session_id=proposed["session_id"], message=prose)
        assert "Executed" not in result["answer"]


async def test_a_structured_confirmation_executes(client: httpx.AsyncClient) -> None:
    proposed = await _chat(client, message=PROPOSE)

    result = await _chat(
        client,
        session_id=proposed["session_id"],
        message="ok",
        confirm={"proposal_id": proposed["proposal"]["proposal_id"], "approve": True},
    )

    assert "Executed" in result["answer"]


async def test_a_replayed_confirmation_does_not_execute_twice(
    client: httpx.AsyncClient,
) -> None:
    """A proposal is single-use, so a retried stream cannot double-execute."""
    proposed = await _chat(client, message=PROPOSE)
    confirm = {"proposal_id": proposed["proposal"]["proposal_id"], "approve": True}

    first = await _chat(client, session_id=proposed["session_id"], message="ok", confirm=confirm)
    second = await _chat(client, session_id=proposed["session_id"], message="ok", confirm=confirm)

    assert "Executed" in first["answer"]
    assert "Executed" not in second["answer"]


async def test_an_unrelated_turn_cancels_a_pending_proposal(
    client: httpx.AsyncClient,
) -> None:
    """docs/00-plan.md §4 Q3: a stale "yes" must have nothing left to point at."""
    proposed = await _chat(client, message=PROPOSE)
    proposal_id = proposed["proposal"]["proposal_id"]

    await _chat(client, session_id=proposed["session_id"], message="actually, never mind")
    late = await _chat(
        client,
        session_id=proposed["session_id"],
        message="ok",
        confirm={"proposal_id": proposal_id, "approve": True},
    )

    assert "Executed" not in late["answer"]


async def test_a_mismatched_proposal_id_executes_nothing(client: httpx.AsyncClient) -> None:
    proposed = await _chat(client, message=PROPOSE)

    result = await _chat(
        client,
        session_id=proposed["session_id"],
        message="ok",
        confirm={"proposal_id": "prop_not_the_one", "approve": True},
    )

    assert "Executed" not in result["answer"]


async def test_declining_a_proposal_executes_nothing(client: httpx.AsyncClient) -> None:
    proposed = await _chat(client, message=PROPOSE)

    result = await _chat(
        client,
        session_id=proposed["session_id"],
        message="no thanks",
        confirm={"proposal_id": proposed["proposal"]["proposal_id"], "approve": False},
    )

    assert "Executed" not in result["answer"]


# ── Channels and options ──────────────────────────────────────────────────────


async def test_a_customer_key_cannot_reach_the_business_route(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/v1/query", headers=auth(CUSTOMER_KEY), json={"query": "x"})

    assert response.status_code == httpx.codes.FORBIDDEN
    assert response.json()["type"].endswith("/channel-mismatch")


async def test_a_business_key_cannot_reach_the_customer_route(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/v1/chat", headers=auth(BUSINESS_KEY), json={"message": "x"})

    assert response.status_code == httpx.codes.FORBIDDEN
    assert response.json()["type"].endswith("/channel-mismatch")


async def test_a_key_without_the_write_scope_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat", headers=auth(NO_SCOPE_KEY), json={"message": "x"})

    assert response.status_code == httpx.codes.FORBIDDEN
    assert response.json()["type"].endswith("/insufficient-scope")


async def test_the_business_channel_answers_with_structured_output(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/query",
        headers=auth(BUSINESS_KEY),
        json={
            "query": "revenue by region",
            "output_schema": {
                "type": "object",
                "properties": {"region": {"type": "string"}, "total": {"type": "number"}},
            },
        },
    )

    structured = response.json()["structured"]
    assert structured is not None
    assert structured["requested_schema_keys"] == ["region", "total"]


async def test_redact_pii_is_refused_rather_than_ignored_on_the_customer_channel(
    client: httpx.AsyncClient,
) -> None:
    """A caller who set it and was quietly ignored would believe redaction was off."""
    response = await client.post(
        "/v1/chat",
        headers=auth(CUSTOMER_KEY),
        json={"message": "x", "options": {"redact_pii": False}},
    )

    assert response.status_code == httpx.codes.BAD_REQUEST
    assert response.json()["type"].endswith("/malformed-request")


async def test_an_unknown_option_is_a_422_not_a_silent_ignore(
    client: httpx.AsyncClient,
) -> None:
    """``extra="forbid"``: an ignored ``max_cost_usd`` would be a caller believing
    they had capped their spend when they had not."""
    response = await client.post(
        "/v1/chat",
        headers=auth(CUSTOMER_KEY),
        json={"message": "x", "options": {"max_cost_uds": 0.01}},
    )

    assert response.status_code == httpx.codes.UNPROCESSABLE_ENTITY


async def test_an_empty_message_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/chat", headers=auth(CUSTOMER_KEY), json={"message": ""})

    assert response.status_code == httpx.codes.UNPROCESSABLE_ENTITY


async def test_the_trace_can_be_switched_off(client: httpx.AsyncClient) -> None:
    with_trace = await _chat(client, message="x")
    without = await _chat(client, message="x", options={"include_trace": False})

    assert with_trace["trace"] is not None
    assert without["trace"] is None


async def test_stubbed_stages_are_reported_as_skipped_not_ok(
    client: httpx.AsyncClient,
) -> None:
    """A client integrating against the echo engine must be able to tell a stubbed
    turn from a real one, so this engine can never be mistaken for working software."""
    result = await _chat(client, message="x")
    by_stage = {entry["stage"]: entry for entry in result["trace"]}

    for stage in ("cache", "classify", "gather", "synthesise"):
        assert by_stage[stage]["status"] == "skipped"
        assert by_stage[stage]["detail"]


async def test_the_turn_status_is_reported_as_a_header(client: httpx.AsyncClient) -> None:
    """The access log reads the turn's own outcome, because a stream's status line
    cannot carry it. The non-streaming path sets it too, so one query covers both."""
    response = await client.post("/v1/chat", headers=auth(CUSTOMER_KEY), json={"message": "x"})

    assert response.headers["X-Turn-Status"] == "ok"
