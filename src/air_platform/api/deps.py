"""Request dependencies: who is calling, and may they call this?

The single most important function here is :func:`require_principal`. It is the
one place a request becomes an identity, and therefore the one place a channel is
decided (docs/01-hld.md §3). Everything downstream — guardrail profile, output
contract, audit sink, tool allow-list — reads the channel off the
:class:`~air_platform.schemas.common.Principal` this produces.

``require_channel`` is a separate guard from ``require_scope`` on purpose. A
customer key reaching ``/v1/query`` is a different mistake from a key missing a
scope, and telling an integrator "wrong channel" points them at the credential
they used rather than at the scopes they granted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import Depends, Header, Request

from air_platform.api.errors import (
    ChannelMismatchError,
    InsufficientScopeError,
    MissingApiKeyError,
)
from air_platform.api.middleware import KEY_ID_ATTR
from air_platform.clients.infra import InfraClient
from air_platform.config import Settings
from air_platform.constants import Channel
from air_platform.memory.session import InMemorySessionStore
from air_platform.observability.logging import bind_request_context, get_logger
from air_platform.schemas.common import Principal
from air_platform.security.api_keys import ApiKeyStore

__all__ = [
    "AppState",
    "get_app_state",
    "get_settings_dep",
    "require_business",
    "require_customer",
    "require_principal",
    "require_scope",
]

_log = get_logger(__name__)

#: Attribute on ``app.state`` holding the assembled :class:`AppState`.
STATE_ATTR: Final[str] = "air_state"


@dataclass(frozen=True, slots=True)
class AppState:
    """Everything the routes need, assembled once at startup.

    A frozen dataclass rather than loose ``app.state`` attributes: a route reading
    ``app.state.key_stroe`` would get an ``AttributeError`` at request time, while a
    typo'd field here fails at import.
    """

    settings: Settings
    key_store: ApiKeyStore
    infra: InfraClient
    sessions: InMemorySessionStore


def get_app_state(request: Request) -> AppState:
    """The process-wide state. Raises if the app was not built by ``create_app``."""
    state = getattr(request.app.state, STATE_ATTR, None)
    if not isinstance(state, AppState):  # pragma: no cover — a wiring defect, not a request error
        raise RuntimeError("application state is not installed; build the app with create_app()")
    return state


def get_settings_dep(state: Annotated[AppState, Depends(get_app_state)]) -> Settings:
    """Settings, via the app rather than the process singleton.

    Routes depend on this instead of ``get_settings()`` so a test can drive a fully
    assembled app with bespoke settings without clearing a global cache.
    """
    return state.settings


async def require_principal(
    request: Request,
    state: Annotated[AppState, Depends(get_app_state)],
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Principal:
    """Resolve the caller, or refuse the request.

    Side effects are deliberate and both are about traceability:

    * the key id is stamped on ``request.state`` so the access-log middleware —
      which runs outside the dependency system — can attribute the request;
    * ``key_id``, ``tenant`` and ``channel`` are bound onto the logging context, so
      every subsequent log line in this turn carries them without being passed them.

    ``tenant`` in particular is bound here rather than at first use: a log line that
    cannot say which tenant it belongs to is useless for investigating the one class
    of bug this service most fears.
    """
    record = state.key_store.lookup(api_key)
    if record is None:
        # No detail about *why* — absent, unknown, and revoked are one answer to a
        # caller, and telling them apart is an oracle for probing the key space.
        raise MissingApiKeyError("A valid X-API-Key header is required.")

    principal = record.to_principal()

    request.state.__setattr__(KEY_ID_ATTR, principal.key_id)
    bind_request_context(
        key_id=principal.key_id,
        tenant=principal.tenant,
        channel=principal.channel.value,
    )
    return principal


def require_scope(scope: str) -> object:
    """Dependency factory guarding one scope.

    Returns a dependency rather than taking the scope at request time so that the
    requirement is visible in the route declaration, and therefore in the generated
    OpenAPI, rather than buried in a handler body.
    """

    async def guard(
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> Principal:
        if not principal.has_scope(scope):
            _log.info("authz.scope_denied", key_id=principal.key_id, required=scope)
            raise InsufficientScopeError(f"This key lacks the '{scope}' scope.")
        return principal

    return Depends(guard)


def require_channel(channel: Channel) -> object:
    """Dependency factory pinning a route to one channel.

    The customer and business routes are separate paths precisely so that this can
    be a declaration rather than a branch inside a shared handler — the two profiles
    then cannot be confused by a future edit to a single code path.
    """

    async def guard(
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> Principal:
        if principal.channel is not channel:
            _log.info(
                "authz.channel_mismatch",
                key_id=principal.key_id,
                key_channel=principal.channel.value,
                route_channel=channel.value,
            )
            raise ChannelMismatchError(
                f"This route serves the {channel.value} channel; "
                f"this key belongs to the {principal.channel.value} channel."
            )
        return principal

    return Depends(guard)


#: Ready-made guards for the two conversational surfaces. Named rather than
#: constructed inline at each route so that the set of channel-pinned routes is
#: greppable.
require_customer: object = require_channel(Channel.CUSTOMER)
require_business: object = require_channel(Channel.BUSINESS)
