"""The application factory — where every part of the service is joined up.

Two properties of this module matter more than the wiring itself.

**The factory takes its settings as an argument.** ``create_app(settings)`` never
reaches for the environment on its own, so a test can assemble a complete
application — real middleware, real error handlers, real auth — without exporting a
variable or clearing a cache. The process singleton is only the default.

**A missing downstream is a deployment shape, not a failure.** air-platform is
designed to narrow its answer rather than refuse one (docs/01-hld.md §7), and that
promise has to hold at *startup* as well as per request: a deployment with no
air-rag, no air-tools and no air-action must boot, serve, and report itself
honestly on ``/v1/ready`` and ``/v1/capabilities``. Since most of the estate is
still unbuilt, that is the normal case rather than the degraded one.

The only things that can stop this service from starting are the ones no turn could
survive: settings that will not validate, or an API key file that will not parse.
Notably **air-infra being down is not one of them** — the service starts, reports
itself unready, and becomes ready when the gateway returns. Refusing to boot would
turn a recoverable dependency outage into a crash loop.

Startup order is not arbitrary: logging comes up before anything that logs its own
status line, and the key store before the routes that authenticate against it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import orjson
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from air_platform import __version__
from air_platform.api.deps import STATE_ATTR, AppState
from air_platform.api.errors import install_error_handlers
from air_platform.api.middleware import install_middleware
from air_platform.api.v1.router import build_v1_router
from air_platform.api.v1.system import build_metrics_router
from air_platform.clients.infra import InfraClient
from air_platform.config import Settings, get_settings
from air_platform.observability.logging import configure_logging, get_logger
from air_platform.security.api_keys import ApiKeyStore

__all__ = ["OrjsonResponse", "app", "create_app"]

_log = get_logger(__name__)

_TITLE: Final[str] = "AIR Platform"

_DESCRIPTION: Final[str] = """
The AIR estate's conversational front door.

Send a message; the platform decides what it needs, gathers it from the specialist
services, and streams back a grounded answer. It orchestrates and owns no
capability of its own — retrieval is `air-rag`'s, classification
`air-classifier`'s, read-only calls `air-tools`', mutations `air-action`'s, and
models and stores `air-infra`'s.

**Two channels, one engine.** `POST /v1/chat` serves public conversational traffic
through the customer gateway; `POST /v1/query` serves internal business queries
through the corporate one, returning schema-validated structured output. They share
the pipeline and differ only by profile — guardrails, output contract, audit sink,
quota bucket, tool allow-list. The channel comes from your API key, not from a
header.

**Mutations are proposed, never performed.** A turn that warrants a write returns a
`proposal` and changes nothing. Executing it takes a second turn carrying
`confirm.proposal_id`; prose that merely reads as agreement executes nothing.

**Streaming.** Responses are Server-Sent Events, content-negotiated: send
`Accept: text/event-stream` to stream, anything else to receive the terminal result
as one JSON body. Every pipeline stage emits as it completes, bracketed by
`turn.start` and `turn.end`. Ignore event names you do not recognise — that is what
lets token deltas arrive later without a contract change.

Errors are RFC 9457 `application/problem+json`.
""".strip()


# ── Serialisation ─────────────────────────────────────────────────────────────


class OrjsonResponse(JSONResponse):
    """``application/json`` rendered by orjson rather than the stdlib encoder.

    Written here rather than imported from ``fastapi.responses``: FastAPI ships the
    same class but deprecated it in 0.141, so using it would emit a warning on every
    single response.

    One trade-off is worth knowing before changing this. Setting *any* custom
    ``default_response_class`` opts the whole app out of FastAPI's newer fast path,
    which serialises a response model straight to bytes through pydantic's Rust core.
    Naming a class here therefore buys a uniform encoder at the cost of that
    shortcut. It is the right trade for this service — the expensive part of a turn
    is the model call, not the encoder — but if response latency ever shows up here,
    dropping this argument is the fix.
    """

    def render(self, content: Any) -> bytes:
        # Non-str keys: a business query's `output_schema` and a proposal's
        # `arguments` are caller-defined maps, and a JSON object key that arrived as
        # an int must not raise on the way out.
        return orjson.dumps(content, option=orjson.OPT_NON_STR_KEYS)


# ── Lifespan ──────────────────────────────────────────────────────────────────


def _build_lifespan(state: AppState) -> Any:
    """Lifespan bound to an already-assembled state.

    Everything fallible is built in :func:`create_app`, before the server starts, so
    a configuration error surfaces as an exception from the factory rather than as a
    half-started application. This hook owns only what must be released.
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        settings = state.settings
        _log.info(
            "startup.complete",
            version=__version__,
            env=settings.app.env,
            port=settings.app.port,
            api_keys=len(state.key_store),
            routes=sorted(r.value for r in settings.downstream.routes()),
            session_backend=settings.session.backend,
            cache_enabled=settings.cache.enabled,
        )
        try:
            yield
        finally:
            # The connection pool outlives individual requests, so it is this
            # hook's job to close it; leaving it open logs an "unclosed client"
            # warning on every reload and leaks sockets in tests.
            await state.infra.aclose()
            _log.info("shutdown.complete")

    return lifespan


# ── Factory ───────────────────────────────────────────────────────────────────


def create_app(settings: Settings | None = None) -> FastAPI:
    """Assemble the application.

    ``settings`` defaults to the process singleton; pass one explicitly in tests.
    """
    resolved = settings or get_settings()
    configure_logging(resolved)

    # Fallible construction, before the server exists. An unparseable key file is a
    # deployment defect and must fail loudly at boot, not as a 401 in production.
    key_store = ApiKeyStore.load(resolved)

    state = AppState(
        settings=resolved,
        key_store=key_store,
        infra=InfraClient(resolved),
    )

    app = FastAPI(
        title=_TITLE,
        description=_DESCRIPTION,
        version=__version__,
        root_path=resolved.app.root_path,
        docs_url="/docs" if resolved.app.docs_enabled else None,
        redoc_url="/redoc" if resolved.app.docs_enabled else None,
        openapi_url="/openapi.json" if resolved.app.docs_enabled else None,
        default_response_class=OrjsonResponse,
        lifespan=_build_lifespan(state),
    )

    # Reachable from any request via ``api.deps.get_app_state``.
    setattr(app.state, STATE_ATTR, state)

    install_middleware(app, resolved)
    install_error_handlers(app)
    app.include_router(build_v1_router())
    if resolved.obs.metrics_enabled:
        app.include_router(build_metrics_router(resolved.obs.metrics_path))

    return app


#: Module-level app for ``uvicorn air_platform.main:app``.
#:
#: Built at import time, which is what uvicorn's reloader and most deployment
#: tooling expect. Tests should call :func:`create_app` instead so they neither
#: depend on the ambient environment nor share this instance's state.
app = create_app()
