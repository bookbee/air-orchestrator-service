"""``GET`` / ``DELETE /v1/sessions/{session_id}``.

Ownership is enforced by the store, and a session belonging to a different principal
404s identically to one that does not exist. Returning 403 for someone else's session
would confirm that the id is live, which turns this endpoint into an oracle for
enumerating sessions.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from starlette.status import HTTP_204_NO_CONTENT

from air_platform.api.deps import AppState, get_app_state, require_principal
from air_platform.api.errors import InsufficientScopeError, SessionNotFoundError
from air_platform.constants import SCOPE_SESSION_READ
from air_platform.schemas.common import Principal
from air_platform.schemas.session import SessionView

router = APIRouter(tags=["sessions"])

_SessionId = Annotated[str, Path(max_length=128, description="The id returned on `turn.start`.")]


def _require_session_scope(principal: Principal) -> None:
    if not principal.has_scope(SCOPE_SESSION_READ):
        raise InsufficientScopeError(f"This key lacks the '{SCOPE_SESSION_READ}' scope.")


@router.get(
    "/sessions/{session_id}",
    response_model=SessionView,
    summary="Read a session's history",
    description="Yours only. Someone else's session is indistinguishable from a missing one.",
)
async def get_session(
    session_id: _SessionId,
    state: Annotated[AppState, Depends(get_app_state)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> SessionView:
    _require_session_scope(principal)
    session = await state.sessions.get(session_id, principal)
    if session is None:
        raise SessionNotFoundError("No such session.")
    return SessionView.of(session)


@router.delete(
    "/sessions/{session_id}",
    status_code=HTTP_204_NO_CONTENT,
    summary="Clear a session",
    description="Drops the history and any proposal awaiting confirmation.",
)
async def delete_session(
    session_id: _SessionId,
    state: Annotated[AppState, Depends(get_app_state)],
    principal: Annotated[Principal, Depends(require_principal)],
) -> None:
    _require_session_scope(principal)
    if not await state.sessions.delete(session_id, principal):
        # Same 404 as a missing session: "not yours" and "not there" are one answer.
        raise SessionNotFoundError("No such session.")
