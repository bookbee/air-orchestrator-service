"""Conversation state.

A session belongs to exactly one principal and one tenant, and both are stored on
it. Ownership is checked on every read: a session belonging to someone else 404s
identically to one that does not exist, so the response cannot be used to probe for
live session ids (docs/02-lld.md §3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from air_platform.constants import Channel


def _now() -> datetime:
    return datetime.now(UTC)


class Turn(BaseModel):
    """One exchange, as it will be replayed into a later prompt.

    Stores the **redacted** text, never the raw input. The guardrail stage redacts
    on the way in, and persisting the original afterwards would put back exactly what
    redaction removed.
    """

    turn_id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime = Field(default_factory=_now)


class PendingProposal(BaseModel):
    """A mutation awaiting confirmation — docs/01-hld.md §6.

    Held on the session rather than in the client's hands so that confirming requires
    something the server is already holding, and so that a proposal can expire.
    """

    proposal_id: str
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    risk: str = "low"
    summary: str = ""
    expires_at: datetime

    def is_expired(self, *, at: datetime | None = None) -> bool:
        return (at or _now()) >= self.expires_at

    @classmethod
    def create(
        cls,
        *,
        proposal_id: str,
        action: str,
        arguments: dict[str, Any],
        risk: str,
        summary: str,
        ttl_seconds: int,
    ) -> PendingProposal:
        return cls(
            proposal_id=proposal_id,
            action=action,
            arguments=arguments,
            risk=risk,
            summary=summary,
            expires_at=_now() + timedelta(seconds=ttl_seconds),
        )


class Session:
    """A conversation. Deliberately a plain class, not a pydantic model.

    It is mutated in place by the store and never crosses the API boundary — what a
    caller sees is :class:`SessionView`. Keeping the two apart is what stops an
    internal field (the owner's key id, say) from being serialised into a response by
    someone adding a field in the wrong place.
    """

    __slots__ = (
        "channel",
        "created_at",
        "owner_key_id",
        "pending",
        "session_id",
        "tenant",
        "turns",
        "updated_at",
    )

    def __init__(
        self,
        *,
        session_id: str,
        owner_key_id: str,
        tenant: str,
        channel: Channel,
    ) -> None:
        self.session_id = session_id
        self.owner_key_id = owner_key_id
        self.tenant = tenant
        self.channel = channel
        self.turns: list[Turn] = []
        self.pending: PendingProposal | None = None
        self.created_at = _now()
        self.updated_at = self.created_at

    def append(self, turn: Turn, *, window: int) -> None:
        """Add a turn and keep the history bounded.

        Trimming here rather than at read time means the stored size is bounded too,
        which is what stops a long-lived session from growing without limit in Redis.
        """
        self.turns.append(turn)
        if len(self.turns) > window:
            # Phase 2 summarises what falls out of the window; v1 drops it, and
            # docs/02-lld.md §16 records that as a marked TODO rather than a silent
            # behaviour.
            del self.turns[: len(self.turns) - window]
        self.updated_at = _now()

    def owned_by(self, key_id: str, tenant: str) -> bool:
        """Both halves are checked. A key id is unique, but the tenant is the
        isolation boundary, and asserting it here means a future store that keys on
        session id alone still cannot cross tenants."""
        return self.owner_key_id == key_id and self.tenant == tenant


class SessionView(BaseModel):
    """What ``GET /v1/sessions/{id}`` returns.

    Carries no owner key id and no tenant: the caller already knows both, and putting
    them on the wire would leak the isolation key into logs and browser histories.
    """

    session_id: str
    channel: Channel
    turns: list[Turn]
    has_pending_proposal: bool = Field(
        description="Whether a mutation is awaiting confirmation. The id is not repeated here."
    )
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, session: Session) -> SessionView:
        return cls(
            session_id=session.session_id,
            channel=session.channel,
            turns=list(session.turns),
            has_pending_proposal=session.pending is not None and not session.pending.is_expired(),
            created_at=session.created_at,
            updated_at=session.updated_at,
        )
