"""The in-memory session store: ownership, and now expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import Channel
from air_orchestrator_service.memory.session import InMemorySessionStore
from air_orchestrator_service.schemas.common import Principal

PRINCIPAL = Principal(key_id="k1", name="k1", channel=Channel.CUSTOMER, tenant="tenant-a")


async def test_a_fresh_session_is_not_expired() -> None:
    store = InMemorySessionStore(ttl_seconds=3600, key_prefix="test:")
    created = await store.create(PRINCIPAL)

    found = await store.get(created.session_id, PRINCIPAL)

    assert found is not None
    assert found.session_id == created.session_id


async def test_an_expired_session_reads_as_absent() -> None:
    """Expired reads exactly like unknown — both are "start a new one" to
    the caller, so the two must be indistinguishable here too."""
    store = InMemorySessionStore(ttl_seconds=1, key_prefix="test:")
    created = await store.create(PRINCIPAL)
    # Backdate rather than sleeping — the same technique `PendingProposal`'s
    # own tests use for expiry, and it makes the test instant.
    created.updated_at = datetime.now(UTC) - timedelta(seconds=10)
    await store.save(created)

    found = await store.get(created.session_id, PRINCIPAL)

    assert found is None


async def test_an_expired_session_is_dropped_from_the_store() -> None:
    store = InMemorySessionStore(ttl_seconds=1, key_prefix="test:")
    created = await store.create(PRINCIPAL)
    created.updated_at = datetime.now(UTC) - timedelta(seconds=10)
    await store.save(created)

    assert len(store) == 1
    await store.get(created.session_id, PRINCIPAL)
    assert len(store) == 0


async def test_total_cost_starts_at_zero() -> None:
    store = InMemorySessionStore(ttl_seconds=3600, key_prefix="test:")
    session = await store.create(PRINCIPAL)

    assert session.total_cost_usd == 0.0


async def test_a_session_is_invisible_to_another_subject(settings: Settings) -> None:
    """``REQ-CTK-007``, AC-5. At the store: a session made for ``cust_1001`` is absent to
    ``cust_1002`` and to an anonymous caller on the same key, and an anonymous one is
    absent to ``cust_1001``. Through the routes: a turn, ``GET`` and ``DELETE`` from
    another customer get the same 404 as a session that does not exist."""
    import httpx
    from fastapi import FastAPI

    from tests.conftest import CUSTOMER_KEY, auth, build_app
    from tests.unit._customer_token_support import configured_key, mint

    def as_(subject: str | None) -> Principal:
        return Principal(
            key_id="k", name="k", channel=Channel.CUSTOMER, tenant="t", subject=subject
        )

    store = InMemorySessionStore(ttl_seconds=600, key_prefix="test:")
    signed_in = await store.create(as_("cust_1001"))
    anonymous = await store.create(as_(None))
    assert await store.get(signed_in.session_id, as_("cust_1001")) is signed_in
    assert await store.get(signed_in.session_id, as_("cust_1002")) is None
    assert await store.get(signed_in.session_id, as_(None)) is None
    assert await store.get(anonymous.session_id, as_("cust_1001")) is None
    assert await store.get(anonymous.session_id, as_(None)) is anonymous

    security = settings.security.model_copy(update={"customer_tokens": [configured_key()]})
    app: FastAPI = build_app(settings.model_copy(update={"security": security}))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:

        def who(subject: str | None) -> dict[str, str]:
            return {**auth(CUSTOMER_KEY), **({"air-token": mint(sub=subject)} if subject else {})}

        started = await http.post("/v1/chat", headers=who("cust_1001"), json={"message": "hi"})
        assert started.status_code == httpx.codes.OK, started.text
        mine = started.json()["session_id"]
        missing = "sess_00000000000000000000000000000000"

        for intruder in ("cust_1002", None):
            for session_id in (mine, missing):
                turn = await http.post(
                    "/v1/chat",
                    headers=who(intruder),
                    json={"message": "hi", "session_id": session_id},
                )
                read = await http.get(f"/v1/sessions/{session_id}", headers=who(intruder))
                drop = await http.delete(f"/v1/sessions/{session_id}", headers=who(intruder))
                assert [r.status_code for r in (turn, read, drop)] == [404, 404, 404]
                assert read.json()["detail"] == turn.json()["detail"]

        own = await http.get(f"/v1/sessions/{mine}", headers=who("cust_1001"))
        assert own.status_code == httpx.codes.OK
