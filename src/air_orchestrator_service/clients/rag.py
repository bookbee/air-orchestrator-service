"""air-rag client — retrieval for the ``GATHER`` stage, spec 0011.

The contract is air-rag's ``POST /v1/retrieve``: ``{"query", "top_k"}`` in, and
``{"citations": [{"source_id", "snippet", "score", "metadata"}]}`` out. The request is
``extra="forbid"`` there, so this client sends exactly those two fields.

**Why the key is chosen per call.** air-rag has no tenant field. Each of its keys
belongs to one tenant, and a search covers only that tenant's documents. So the
tenant is chosen by choosing the key: ``RagSettings.tenant_keys`` maps this service's
tenants to air-rag keys, and every call uses the principal's (spec 0011,
``REQ-RAG-016``). One unmapped ``api_key`` serves every tenant, which is right only for
a single-tenant deployment.

**Why the response model is loose.** :class:`Passage` reads the four fields the engine
uses and ignores the rest. ``metadata`` is air-rag's free-form dict: ``title`` and
``uri`` are read from it when present, as strings, and never trusted. A passage is a
downstream's text, and so are its title and URL. The engine screens and delimits the
snippet before a model sees it, and :func:`safe_title` / :func:`safe_uri` clean the
two fields before an end user sees them (``REQ-RAG-010``).

**Why no score filter is sent.** air-rag has no minimum-score option, so the engine
filters (``RagSettings.min_score``). It asks for ``top_k`` a little above
``max_passages`` so the filter has something to choose among.

Everything else comes from ``clients/base.py`` (spec 0010): headers, one attempt,
the four failure reasons, the metric and the breaker.
"""

from __future__ import annotations

import re
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from air_orchestrator_service.clients.base import DownstreamClient, DownstreamError
from air_orchestrator_service.config import RagSettings
from air_orchestrator_service.constants import DownstreamService

__all__ = [
    "MIN_CALL_MS",
    "Passage",
    "RagClient",
    "RagError",
    "Retrieval",
    "safe_title",
    "safe_uri",
]

_RETRIEVE_PATH: Final[str] = "/v1/retrieve"
#: The shortest bound worth a call. Below it, air-rag's embedding call alone does not fit.
MIN_CALL_MS: Final[int] = 150
_TITLE_CHARS: Final[int] = 200
_URI_CHARS: Final[int] = 2048
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class RagError(DownstreamError):
    """air-rag produced no usable result. ``reason`` is one of spec 0010's four words."""


class Passage(BaseModel):
    """One of air-rag's ``citations``: a chunk, its score, and its metadata."""

    model_config = ConfigDict(extra="ignore")

    source_id: str
    snippet: str
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def title(self) -> str | None:
        value = self.metadata.get("title")
        return value if isinstance(value, str) else None

    @property
    def uri(self) -> str | None:
        value = self.metadata.get("uri")
        return value if isinstance(value, str) else None


class RetrievalUsage(BaseModel):
    """What air-rag says the call cost, in air-classifier's shape. air-rag sends none
    today, and its local embeddings are free, so this is 0 until it does
    (``REQ-RAG-013``)."""

    model_config = ConfigDict(extra="ignore")

    est_cost_usd: float = Field(default=0.0, ge=0)


class Retrieval(BaseModel):
    """The slice of air-rag's ``RetrievalResponse`` this service reads."""

    model_config = ConfigDict(extra="ignore")

    citations: list[Passage] = Field(default_factory=list)
    usage: RetrievalUsage = Field(default_factory=RetrievalUsage)


def safe_title(passage: Passage) -> str:
    """The source's title, or its id when it has none, without control characters and
    cut to a fixed length."""
    raw = passage.title or passage.source_id
    cleaned = " ".join(_CONTROL.sub(" ", raw).split())
    return cleaned[:_TITLE_CHARS] or "source"


def safe_uri(passage: Passage) -> str | None:
    """The source's URL if it is a plain ``http``/``https`` one, else ``None``. A
    ``javascript:`` or ``data:`` URL rendered as a link is a script."""
    raw = passage.uri
    if raw is None:
        return None
    candidate = raw.strip()
    if _CONTROL.search(candidate) or " " in candidate or len(candidate) > _URI_CHARS:
        return None
    if not re.match(r"^https?://[^/\s]+", candidate, flags=re.IGNORECASE):
        return None
    return candidate


class RagClient(DownstreamClient):
    """Thin async client for air-rag. See the module docstring."""

    def __init__(self, config: RagSettings) -> None:
        super().__init__(DownstreamService.RAG, config)
        self.settings = config

    async def retrieve(self, query: str, *, tenant: str, timeout_s: float) -> Retrieval:
        """Passages for ``query`` (already input-masked by the caller) from ``tenant``'s
        knowledge base, or raise :class:`RagError`. The caller checks
        :meth:`RagSettings.serves` first; a tenant with no key is a caller bug here."""
        if not self.settings.serves(tenant):
            raise ValueError("this tenant has no air-rag key")
        body: dict[str, Any] = {"query": query[:4000], "top_k": self.settings.top_k}
        return await self.post_json(
            _RETRIEVE_PATH,
            body,
            timeout_s=timeout_s,
            parse=Retrieval.model_validate,
            api_key=self.settings.key_for(tenant),
        )

    def _error(self, message: str, *, reason: str, status: int | None) -> DownstreamError:
        return RagError(message, service=self.service, reason=reason, status=status)
