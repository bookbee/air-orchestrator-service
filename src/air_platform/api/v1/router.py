"""Aggregates the v1 routers.

Phase 0 mounts the system surfaces only. The conversational routes — ``/v1/chat``
(customer) and ``/v1/query`` (business) — land in Phase 1 alongside the SSE event
contract, and ``/v1/sessions/{id}`` with them.

They are deliberately kept as two paths pinned to two channels via
``api.deps.require_customer`` / ``require_business``, rather than one handler that
branches on the principal: a shared handler is how a later edit accidentally
applies the customer guardrail profile to business traffic, or the reverse.
"""

from __future__ import annotations

from fastapi import APIRouter

from air_platform.api.v1 import system
from air_platform.constants import V1_PREFIX

__all__ = ["build_v1_router"]


def build_v1_router() -> APIRouter:
    """The mounted ``/v1`` surface."""
    v1 = APIRouter(prefix=V1_PREFIX)
    v1.include_router(system.router)
    return v1
