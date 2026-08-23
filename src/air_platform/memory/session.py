"""Session storage.

The store is a protocol with an in-memory implementation. Redis is the Phase 2
backend and slots in behind the same three methods, which is what makes
``session.backend`` a configuration change rather than a code change.

**Every key is namespaced by tenant.** Not because the in-memory store needs it —
it holds one dict per process — but because the key shape is the thing Redis will
inherit, and a store keyed on session id alone would let a guessed id cross a tenant
boundary. Getting that wrong is the single hardest class of bug to detect after the
fact, so the namespace exists before the backend that needs it.
"""

from __future__ import annotations

import uuid
from typing import Final, Protocol

from air_platform.config import Settings
from air_platform.observability.logging import get_logger
from air_platform.schemas.common import Principal
from air_platform.schemas.session import PendingProposal, Session

__all__ = ["InMemorySessionStore", "SessionStore", "build_session_store", "new_session_id"]

logger = get_logger(__name__)

_SESSION_ID_PREFIX: Final[str] = "sess_"


def new_session_id() -> str:
    return f"{_SESSION_ID_PREFIX}{uuid.uuid4().hex}"


class SessionStore(Protocol):
    """What the engine needs of conversation state, and nothing more."""

    async def get(self, session_id: str, principal: Principal) -> Session | None:
        """The session, or ``None`` if it is absent **or** not this principal's.

        The two cases are deliberately indistinguishable to the caller: the route
        turns both into the same 404, so the endpoint cannot be used to discover
        which session ids exist.
        """
        ...

    async def create(self, principal: Principal, *, session_id: str | None = None) -> Session: ...

    async def save(self, session: Session) -> None: ...

    async def delete(self, session_id: str, principal: Principal) -> bool:
        """``True`` if something was removed. ``False`` reads as "not yours or not there"."""
        ...


class InMemorySessionStore:
    """Single-process store.

    Correct for a laptop and for the tests, and wrong for anything autoscaled — which
    is why ``config`` refuses a Redis backend without a URL rather than falling back
    here. A conversation that forgets itself whenever the load balancer moves is the
    failure that looks like it works.
    """

    def __init__(self, *, ttl_seconds: int, key_prefix: str) -> None:
        self._ttl_seconds = ttl_seconds
        self._key_prefix = key_prefix
        self._sessions: dict[str, Session] = {}

    def _key(self, tenant: str, session_id: str) -> str:
        """The key shape Redis will inherit. Tenant first, so a scan is tenant-scoped."""
        return f"{self._key_prefix}{tenant}:session:{session_id}"

    async def get(self, session_id: str, principal: Principal) -> Session | None:
        session = self._sessions.get(self._key(principal.tenant, session_id))
        if session is None:
            return None
        if not session.owned_by(principal.key_id, principal.tenant):
            # Reachable only if two principals in one tenant share a session id, which
            # the id space makes vanishingly unlikely — but the check is what makes
            # ownership a property of the store rather than of the caller's diligence.
            logger.warning("session.owner_mismatch", session_id=session_id)
            return None
        return session

    async def create(self, principal: Principal, *, session_id: str | None = None) -> Session:
        session = Session(
            session_id=session_id or new_session_id(),
            owner_key_id=principal.key_id,
            tenant=principal.tenant,
            channel=principal.channel,
        )
        await self.save(session)
        return session

    async def save(self, session: Session) -> None:
        self._sessions[self._key(session.tenant, session.session_id)] = session

    async def delete(self, session_id: str, principal: Principal) -> bool:
        session = await self.get(session_id, principal)
        if session is None:
            return False
        del self._sessions[self._key(principal.tenant, session_id)]
        return True

    # ── Proposals ─────────────────────────────────────────────────────────────

    async def set_pending(self, session: Session, proposal: PendingProposal) -> None:
        session.pending = proposal
        await self.save(session)

    async def take_pending(self, session: Session, proposal_id: str) -> PendingProposal | None:
        """Consume the pending proposal if it matches and has not expired.

        Consuming rather than reading is deliberate: a proposal is single-use, so a
        replayed confirmation finds nothing and the idempotency key never has to be
        the only thing standing between a retry and a double execution.
        """
        pending = session.pending
        session.pending = None
        await self.save(session)
        if pending is None or pending.proposal_id != proposal_id or pending.is_expired():
            return None
        return pending

    async def clear_pending(self, session: Session) -> None:
        """Any turn that does not answer the proposal cancels it (docs/00-plan.md §4 Q3)."""
        if session.pending is not None:
            session.pending = None
            await self.save(session)

    def __len__(self) -> int:
        return len(self._sessions)


def build_session_store(settings: Settings) -> InMemorySessionStore:
    """Construct the configured backend.

    Returns the in-memory store for both settings today. The Redis backend is Phase 2
    (docs/02-lld.md §16); until it exists, selecting it and silently getting memory
    would be exactly the failure ``config`` guards against, so this logs loudly.
    """
    if settings.session.backend == "redis":
        logger.warning(
            "session.redis_backend_not_implemented",
            detail="falling back to in-memory; sessions will not survive a restart "
            "and are not shared across replicas",
        )
    return InMemorySessionStore(
        ttl_seconds=settings.session.ttl_seconds,
        key_prefix=settings.session.key_prefix,
    )
