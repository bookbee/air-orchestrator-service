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

from air_orchestrator_service.constants import Channel


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
    #: The air-tools-gateway proposal this one stands for, or ``None`` for the
    #: ``/propose`` stand-in. Held here and nowhere else: the caller only ever sees
    #: ``proposal_id`` (spec 0012, ``REQ-TGW-013``). Out of ``repr`` so a log line
    #: or traceback that prints the model does not carry it either.
    gateway_proposal_id: str | None = Field(default=None, repr=False)
    #: Who created it, so only the same key and customer can confirm it (spec 0012
    #: ``REQ-TGW-019``, enforced at execute).
    key_id: str | None = None
    subject: str | None = Field(default=None, repr=False)

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
        not_after: datetime | None = None,
        gateway_proposal_id: str | None = None,
        key_id: str | None = None,
        subject: str | None = None,
    ) -> PendingProposal:
        """``not_after`` caps the expiry: the earlier of it and ``ttl_seconds`` from now
        wins (spec 0012, ``REQ-TGW-014``)."""
        expires_at = _now() + timedelta(seconds=ttl_seconds)
        if not_after is not None:
            expires_at = min(expires_at, not_after)
        return cls(
            proposal_id=proposal_id,
            action=action,
            arguments=arguments,
            risk=risk,
            summary=summary,
            expires_at=expires_at,
            gateway_proposal_id=gateway_proposal_id,
            key_id=key_id,
            subject=subject,
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
        "total_cost_usd",
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
        #: Cumulative spend across every turn this session has made, checked
        #: against `SessionSettings.max_cost_usd` before the next turn
        #: attempts synthesis (`TurnEngine`). "Log the cost of each
        #: conversation" made checkable per-session, not just aggregated in
        #: Prometheus by tenant.
        self.total_cost_usd: float = 0.0

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
    total_cost_usd: float
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, session: Session) -> SessionView:
        return cls(
            session_id=session.session_id,
            channel=session.channel,
            turns=list(session.turns),
            has_pending_proposal=session.pending is not None and not session.pending.is_expired(),
            total_cost_usd=session.total_cost_usd,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )
