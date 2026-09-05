"""Aggregates the v1 routers.

``/v1/chat`` and ``/v1/query`` are two paths pinned to two channels via
``api.deps.require_customer`` / ``require_business``, rather than one handler that
branches on the principal: a shared handler is how a later edit accidentally applies
the customer guardrail profile to business traffic, or the reverse.

Both are served by the same :class:`~air_orchestrator_service.engine.turn.TurnEngine`.
Later phases fill in the stages it still stubs without touching this file — the
event contract does not change.
"""

from __future__ import annotations

from fastapi import APIRouter

from air_orchestrator_service.api.v1 import chat, query, sessions, system
from air_orchestrator_service.constants import V1_PREFIX

__all__ = ["build_v1_router"]


def build_v1_router() -> APIRouter:
    """The mounted ``/v1`` surface."""
    v1 = APIRouter(prefix=V1_PREFIX)
    v1.include_router(system.router)
    v1.include_router(chat.router)
    v1.include_router(query.router)
    v1.include_router(sessions.router)
    return v1
