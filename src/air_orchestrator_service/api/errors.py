"""The error model — docs/02-lld.md §12.

Two rules hold everywhere in this module, and everything else is plumbing:

* **Every** failure leaves the service as an RFC 9457 ``application/problem+json``
  body. There is no second error shape, no bare ``{"detail": ...}`` from FastAPI's
  defaults, and no HTML from Starlette's. A caller writes one parser.
* **Nothing internal escapes.** The catch-all logs the traceback and returns a
  fixed 500 body. An exception message routinely carries a connection string, a
  file path, or a fragment of user text, and an error response is the least
  guarded surface the service has.

Note what the catalogue does *not* contain: there is no "rag unavailable" or
"tools unavailable". A downstream being down narrows what a turn can answer and is
reported in the turn's own `degraded` list with a 200 (docs/01-hld.md §7). The one
dependency whose absence is fatal is air-llm — without it no answer can be
synthesised — and it lands on 503 ``dependency-unavailable``.

A second asymmetry matters for the streaming routes: a response that has already
sent its headers has no status left to change, so a mid-stream failure is an
`error` **event** rather than a problem body. This module handles only the
failures that happen before the first byte.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any, ClassVar, Final

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import Response

from air_orchestrator_service.constants import HEADER_REQUEST_ID
from air_orchestrator_service.observability.logging import get_logger
from air_orchestrator_service.schemas.errors import FieldError, ProblemDetail

__all__ = [
    "AppError",
    "ChannelMismatchError",
    "DependencyUnavailableError",
    "InsufficientScopeError",
    "InternalError",
    "MalformedRequestError",
    "MissingApiKeyError",
    "NotFoundError",
    "ProblemResponse",
    "ProposalNotFoundError",
    "RateLimitedError",
    "SessionNotFoundError",
    "TurnBudgetExceededError",
    "ValidationError",
    "install_error_handlers",
    "problem_response",
]

_log = get_logger(__name__)

#: RFC 9457 media type. Distinct from ``application/json`` so a client can route
#: on the content type alone rather than on the status code.
PROBLEM_CONTENT_TYPE: Final[str] = "application/problem+json"

#: Loc prefixes pydantic prepends to a field path. ``body`` is stripped so the
#: reported field matches what the caller wrote in their JSON; the others are
#: kept, because ``session_id`` alone would not say whether it was a query
#: parameter or a body field that was rejected.
_STRIPPED_LOC_PREFIXES: Final[frozenset[str]] = frozenset({"body"})


# ── Exception hierarchy ───────────────────────────────────────────────────────


class AppError(Exception):
    """Base for every failure this service reports deliberately.

    Subclasses fix ``slug``/``title``/``status`` as class attributes rather than
    constructor arguments: the catalogue in §12 is a contract, and a per-raise
    status is how two call sites end up disagreeing about what a 403 means.
    """

    slug: ClassVar[str] = "internal-error"
    title: ClassVar[str] = "Internal server error"
    status: ClassVar[int] = 500

    def __init__(
        self,
        detail: str | None = None,
        *,
        errors: Sequence[FieldError] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.errors: list[FieldError] = list(errors or ())
        self.headers: dict[str, str] = dict(headers or {})

    def to_problem(
        self, *, instance: str | None = None, request_id: str | None = None
    ) -> ProblemDetail:
        """Render as the body the caller receives."""
        return ProblemDetail.build(
            slug=self.slug,
            title=self.title,
            status=self.status,
            detail=self.detail,
            instance=instance,
            request_id=request_id,
            errors=self.errors,
        )


class MalformedRequestError(AppError):
    """Unparseable body, or a payload larger than the configured ceiling."""

    slug = "malformed-request"
    title = "Malformed request"
    status = 400


class MissingApiKeyError(AppError):
    """No ``X-API-Key`` header, or one that matches no known key."""

    slug = "missing-api-key"
    title = "Missing or invalid API key"
    status = 401


class InsufficientScopeError(AppError):
    """The key lacks the scope the route requires."""

    slug = "insufficient-scope"
    title = "Insufficient scope"
    status = 403


class ChannelMismatchError(AppError):
    """A customer key reached ``/v1/query``, or a business key reached ``/v1/chat``.

    Its own catalogue entry rather than a generic 403 because it is the error an
    integrator is most likely to hit, and "insufficient scope" would send them
    looking at their scopes instead of at which key they used.
    """

    slug = "channel-mismatch"
    title = "Wrong channel for this route"
    status = 403


class NotFoundError(AppError):
    """Unknown route or unknown resource."""

    slug = "not-found"
    title = "Not found"
    status = 404


class SessionNotFoundError(AppError):
    """No such session, or one owned by a different principal.

    Those two cases are deliberately indistinguishable: returning 403 for someone
    else's session would confirm that the id exists, turning this into an oracle
    for enumerating live sessions.
    """

    slug = "session-not-found"
    title = "Session not found"
    status = 404


class ProposalNotFoundError(AppError):
    """The confirmed ``proposal_id`` is unknown, expired, or already cancelled.

    Also indistinguishable by design: a confirmation citing a proposal that was
    never made and one citing a proposal that has expired should tell an attacker
    exactly the same amount, which is nothing.
    """

    slug = "proposal-not-found"
    title = "Proposal not found or expired"
    status = 404


class ValidationError(AppError):
    """A schema violation. Shadows pydantic's name inside this module only.

    Carries the per-field breakdown in ``errors`` so a client can highlight the
    offending inputs without parsing prose out of ``detail``.
    """

    slug = "validation-error"
    title = "Request validation failed"
    status = 422


class RateLimitedError(AppError):
    """Quota exhausted for this key.

    ``retry_after`` is seconds and is floored at 1 in the header: RFC 9110 wants an
    integer, and a sub-second remainder rounded down to 0 invites a client to retry
    immediately and get denied again.
    """

    slug = "rate-limited"
    title = "Rate limit exceeded"
    status = 429

    def __init__(
        self,
        detail: str | None = None,
        *,
        retry_after: float = 1.0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        merged = {"Retry-After": str(max(1, math.ceil(retry_after)))}
        merged.update(headers or {})
        super().__init__(detail, headers=merged)
        self.retry_after = retry_after


class TurnBudgetExceededError(AppError):
    """The turn's cost or model-call ceiling was hit before it could start.

    429 rather than 400: the request is well-formed and would succeed later or
    under a larger budget. Mid-turn exhaustion never reaches here — it degrades the
    answer instead (docs/02-lld.md §6).
    """

    slug = "turn-budget-exceeded"
    title = "Turn budget exceeded"
    status = 429


class InternalError(AppError):
    """An unhandled defect. Should be alert-worthy and rare."""

    slug = "internal-error"
    title = "Internal server error"
    status = 500


class DependencyUnavailableError(AppError):
    """air-llm is unreachable, so no turn can be synthesised.

    The only 5xx this service raises for a dependency, and reserved for air-llm
    alone — see this module's docstring.
    """

    slug = "dependency-unavailable"
    title = "Required dependency unavailable"
    status = 503


#: Status → catalogue entry, used to give Starlette's own ``HTTPException``s (404
#: from the router, 405 from a method mismatch) the same body shape as ours.
#:
#: Built from a fixed order so that a status claimed by more than one entry
#: resolves predictably: 403 belongs to InsufficientScopeError rather than
#: ChannelMismatchError, and 404 to NotFoundError rather than either of the
#: narrower two, because those are the generic readings a framework-raised error
#: should get.
_BY_STATUS: Final[dict[int, type[AppError]]] = {
    cls.status: cls
    for cls in (
        MalformedRequestError,
        MissingApiKeyError,
        InsufficientScopeError,
        NotFoundError,
        ValidationError,
        RateLimitedError,
        InternalError,
        DependencyUnavailableError,
    )
}


# ── Response construction ─────────────────────────────────────────────────────


class ProblemResponse(JSONResponse):
    """A JSON response typed as ``application/problem+json``."""

    media_type = PROBLEM_CONTENT_TYPE


def problem_response(
    problem: ProblemDetail, *, headers: Mapping[str, str] | None = None
) -> ProblemResponse:
    """Wrap a problem in its response.

    ``exclude_none`` keeps optional RFC 9457 members off the wire rather than
    sending them as nulls — a member that is absent and one that is present but
    null mean the same thing to the spec, and the former is smaller.

    The request id is echoed as a header as well as carried in the body so that a
    proxy, a log scraper, or a client that only reads headers on failures can still
    correlate the response with a trace.
    """
    merged = dict(headers or {})
    if problem.request_id:
        merged.setdefault(HEADER_REQUEST_ID, problem.request_id)
    return ProblemResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json", exclude_none=True),
        headers=merged,
    )


def _instance(request: Request) -> str:
    """The occurrence URI: the path, without the query string.

    Query strings carry caller-supplied values, and an error body is copied into
    tickets and log aggregators far more often than a success body is.
    """
    return request.url.path


def _request_id(request: Request) -> str | None:
    """The id stamped by ``RequestContextMiddleware``, if it is installed.

    This module deliberately does not mint one of its own: a second generator
    would let a response carry an id that appears in no log line.
    """
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def _slug_for_status(status: int) -> tuple[str, str]:
    """Catalogue entry for a status, or one derived from the HTTP phrase.

    The §12 table does not cover every status Starlette can raise (405, 415, …).
    Deriving the slug from the reason phrase keeps those responses in the same
    shape and gives them a stable, guessable ``type`` URI instead of collapsing
    them onto a status the caller did not actually hit.
    """
    known = _BY_STATUS.get(status)
    if known is not None:
        return known.slug, known.title
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        phrase = "HTTP error"
    # Slugs land in a URI, so collapse everything outside [a-z0-9] rather than
    # only spaces — "I'm a Teapot" must not put an apostrophe in a `type`.
    return re.sub(r"[^a-z0-9]+", "-", phrase.lower()).strip("-"), phrase


# ── Handlers ──────────────────────────────────────────────────────────────────


async def _app_error_handler(request: Request, exc: Exception) -> Response:
    """Anything raised deliberately by our own code."""
    assert isinstance(exc, AppError)  # registered for exactly this type
    problem = exc.to_problem(instance=_instance(request), request_id=_request_id(request))
    return problem_response(problem, headers=exc.headers)


async def _validation_error_handler(request: Request, exc: Exception) -> Response:
    """Pydantic's per-field failures, flattened into ``errors``."""
    assert isinstance(exc, RequestValidationError)
    field_errors = [_as_field_error(raw) for raw in exc.errors()]
    first = field_errors[0] if field_errors else None
    detail = f"{first.field}: {first.message}" if first and first.message else None
    return await _app_error_handler(request, ValidationError(detail, errors=field_errors))


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    """Starlette's and FastAPI's ``HTTPException``, re-shaped as a problem.

    Headers are preserved because some are load-bearing on the status they
    accompany — ``Allow`` on a 405, ``WWW-Authenticate`` on a 401.
    """
    assert isinstance(exc, StarletteHTTPException)
    slug, title = _slug_for_status(exc.status_code)
    problem = ProblemDetail.build(
        slug=slug,
        title=title,
        status=exc.status_code,
        detail=str(exc.detail) if exc.detail else None,
        instance=_instance(request),
        request_id=_request_id(request),
    )
    return problem_response(problem, headers=exc.headers or {})


async def _unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """The last line of defence.

    The traceback goes to the log, where it is useful and access-controlled. The
    caller gets a fixed sentence and the request id — enough to open a ticket, not
    enough to learn anything about the inside of the process.
    """
    _log.error(
        "request.unhandled_exception",
        exc_info=exc,
        method=request.method,
        path=_instance(request),
        error_type=type(exc).__name__,
    )
    return await _app_error_handler(
        request,
        InternalError("An unexpected error occurred. Quote the request id when reporting."),
    )


def _as_field_error(raw: Mapping[str, Any]) -> FieldError:
    """One pydantic error dict → one :class:`FieldError`.

    Only ``loc``/``msg``/``type`` are read. ``ctx`` and ``input`` are ignored on
    purpose: ``input`` is the caller's rejected value, and echoing it into an error
    body is how a redaction pipeline gets bypassed by its own 422.
    """
    loc: tuple[Any, ...] = tuple(raw.get("loc", ()))
    if loc and loc[0] in _STRIPPED_LOC_PREFIXES:
        loc = loc[1:]
    return FieldError(
        field=".".join(str(part) for part in loc) or "__root__",
        code=str(raw.get("type", "invalid")),
        message=str(raw["msg"]) if raw.get("msg") else None,
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register every handler on ``app``.

    Registration order does not matter — Starlette dispatches on the most specific
    registered class — but the set does: without the ``Exception`` entry the
    catch-all is uvicorn's, which returns a bare ``Internal Server Error`` in
    ``text/plain``.
    """
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
