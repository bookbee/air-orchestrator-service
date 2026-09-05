"""The in-memory session store: ownership, and now expiry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
