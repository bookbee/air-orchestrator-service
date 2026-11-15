"""The customer token: who the shopper is — spec 0014.

A customer-channel API key names an application and a tenant, not a person. A storefront
that has signed a shopper in sends, beside its key, a short-lived signed JWT in the
``air-token`` header, and :class:`CustomerTokenVerifier` turns it into the principal's
``subject``. Nothing else ever sets it (``REQ-CTK-009``).

**Why the algorithm is pinned by the key, never read from the token.** The classic JWT
bug is a verifier that lets the token's own ``alg`` header choose how it is checked:
``none`` skips the signature, and ``HS256`` with the public key bytes as the "secret"
forges one. Here each configured key carries its algorithm. A header that names any
other is refused as ``bad_algorithm`` before PyJWT is called, and PyJWT is then called
with that one algorithm only (``REQ-CTK-003``). v1 allows ``ES256`` alone.

**Why public keys only.** With an asymmetric algorithm this service holds nothing that
can mint a token, so a leak of its settings forges no customer. A private key supplied by
mistake is refused at startup rather than quietly used to verify (``REQ-CTK-010``).

**Why one uniform refusal.** Every failure raises :class:`CustomerTokenRejectedError` carrying
only its outcome word. The caller gets one fixed ``401`` whatever the reason, so the
answer cannot be used to learn which check a forged token got past; the reason is
counted and logged for the operator (``REQ-CTK-005``, ``012``). The token itself is
never put into an exception, a log call or a repr (``REQ-CTK-008``).

**Why no network call.** Keys are configured, not fetched (§9 Q2), so verification
cannot be slowed or switched off by another service being down (``REQ-CTK-011``).

**Why these limits are constants.** Lifetime, leeway, size and audience are fixed by the
spec. A setting would let a deployment weaken them quietly.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final, Literal

import jwt
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, EllipticCurvePublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from air_orchestrator_service.observability import metrics

__all__ = [
    "AUDIENCE",
    "HEADER",
    "OUTCOMES",
    "TOKEN_TYPE",
    "CustomerTokenKey",
    "CustomerTokenRejectedError",
    "CustomerTokenVerifier",
    "subject_hash",
]

#: The header the token travels in; the name AIR applications outside this repo use.
HEADER: Final[str] = "air-token"
AUDIENCE: Final[str] = "air-orchestrator-service"
TOKEN_TYPE: Final[str] = "air-customer+jwt"  # noqa: S105 — the JWT `typ`, not a secret
MAX_LIFETIME_S: Final[int] = 900
LEEWAY_S: Final[int] = 30
MAX_TOKEN_BYTES: Final[int] = 4096
ALGORITHMS: Final[frozenset[str]] = frozenset({"ES256"})
_SUBJECT: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SEGMENT: Final = re.compile(r"^[A-Za-z0-9_-]+$")

#: Every value ``customer_tokens_total{outcome}`` can take (``REQ-CTK-012``).
OUTCOMES: Final[tuple[str, ...]] = (
    "absent",
    "valid",
    "malformed",
    "unknown_key",
    "bad_algorithm",
    "bad_signature",
    "expired",
    "not_yet_valid",
    "too_long_lived",
    "wrong_issuer",
    "wrong_audience",
    "wrong_tenant",
    "bad_subject",
)


def subject_hash(subject: str) -> str:
    """How a subject may appear in a log line: 12 hex of its sha256 (``REQ-CTK-008``)."""
    return hashlib.sha256(subject.encode()).hexdigest()[:12]


class CustomerTokenRejectedError(Exception):
    """A presented token failed a check. Carries the outcome word and nothing else."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


class CustomerTokenKey(BaseModel):
    """One issuer's verification key for one tenant (``REQ-CTK-010``). ``public_key``
    is a PEM, or a JWK as a JSON object. Loaded and checked when the settings are."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    issuer: str = Field(min_length=1, max_length=256)
    tenant: str = Field(min_length=1, max_length=128)
    kid: str = Field(min_length=1, max_length=128)
    alg: Literal["ES256"]
    public_key: str | dict[str, Any] = Field(repr=False)
    _loaded: EllipticCurvePublicKey | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _load(self) -> CustomerTokenKey:
        self._loaded = _public_key(self.public_key)
        return self

    @property
    def key(self) -> EllipticCurvePublicKey:
        assert self._loaded is not None
        return self._loaded


def _public_key(raw: str | Mapping[str, Any]) -> EllipticCurvePublicKey:
    """A P-256 public key, or ``ValueError``. Never a private one."""
    if isinstance(raw, str):
        if "PRIVATE" in raw:
            raise ValueError("a private key was given; configure the public key only")
        try:
            loaded: object = load_pem_public_key(raw.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("public_key is not a PEM public key") from exc
    else:
        if "d" in raw:
            raise ValueError("a private JWK was given; configure the public key only")
        try:
            loaded = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(dict(raw)))
        except (jwt.InvalidKeyError, ValueError, TypeError, KeyError) as exc:
            raise ValueError("public_key is not an EC JWK") from exc
    if not isinstance(loaded, EllipticCurvePublicKey):
        raise ValueError("public_key must be an EC public key")
    if not isinstance(loaded.curve, SECP256R1):
        raise ValueError("ES256 needs a P-256 key")
    return loaded


class CustomerTokenVerifier:
    """Checks ``air-token`` headers against the configured keys. Built once from
    settings in ``create_app`` (Article VIII). Stateless after construction."""

    def __init__(self, keys: Iterable[CustomerTokenKey]) -> None:
        self._keys: dict[str, CustomerTokenKey] = {}
        for key in keys:
            if key.kid in self._keys:
                raise ValueError(f"duplicate customer token kid {key.kid!r}")
            self._keys[key.kid] = key

    def __repr__(self) -> str:
        return f"CustomerTokenVerifier(kids={sorted(self._keys)!r})"

    def resolve(self, token: str | None, *, tenant: str) -> str | None:
        """The verified subject, ``None`` for no token, or :class:`CustomerTokenRejectedError`.
        Every call is counted with its outcome (``REQ-CTK-012``)."""
        if token is None:
            metrics.record_customer_token(outcome="absent")
            return None
        try:
            subject = self._verify(token, tenant=tenant)
        except CustomerTokenRejectedError as exc:
            metrics.record_customer_token(outcome=exc.outcome)
            raise
        metrics.record_customer_token(outcome="valid")
        return subject

    def kid_of(self, token: str) -> str | None:
        """The token's ``kid`` when it names a configured key, for the rejection log
        line; never anything else from the token."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.InvalidTokenError:
            return None
        return kid if isinstance(kid, str) and kid in self._keys else None

    def _verify(self, token: str, *, tenant: str) -> str:
        # 1. Shape, before any parsing.
        segments = token.split(".")
        if (
            len(token.encode()) > MAX_TOKEN_BYTES
            or len(segments) != 3
            or not all(_SEGMENT.match(segment) for segment in segments[:2])
            # An empty signature is allowed this far, so `alg: none` is refused below
            # as what it is, `bad_algorithm` (spec 0014 AC-2).
            or not _SEGMENT.match(segments[2] or "-")
        ):
            raise CustomerTokenRejectedError("malformed")
        # 2. The unverified header: only what is needed to choose the key.
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            raise CustomerTokenRejectedError("malformed") from None
        if header.get("typ") != TOKEN_TYPE:
            raise CustomerTokenRejectedError("malformed")
        kid = header.get("kid")
        key = self._keys.get(kid) if isinstance(kid, str) else None
        if key is None:
            raise CustomerTokenRejectedError("unknown_key")
        # 3. The key pins the algorithm; the header may only agree with it.
        if header.get("alg") != key.alg or key.alg not in ALGORITHMS:
            raise CustomerTokenRejectedError("bad_algorithm")
        # 4. Signature and the registered claims.
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=[key.alg],
                audience=AUDIENCE,
                issuer=key.issuer,
                leeway=LEEWAY_S,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.InvalidTokenError as exc:
            raise CustomerTokenRejectedError(_outcome(exc)) from None
        # 5. This service's own rules.
        exp, iat = claims["exp"], claims["iat"]
        if not _number(exp) or not _number(iat) or exp - iat > MAX_LIFETIME_S:
            raise CustomerTokenRejectedError("too_long_lived")
        subject = claims["sub"]
        if not isinstance(subject, str) or not _SUBJECT.match(subject):
            raise CustomerTokenRejectedError("bad_subject")
        if claims.get("air_tenant") != key.tenant or key.tenant != tenant:
            raise CustomerTokenRejectedError("wrong_tenant")
        return subject


def _number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _outcome(exc: jwt.InvalidTokenError) -> str:
    """PyJWT's exception, as one outcome word. Subclasses before their parents:
    ``InvalidSignatureError`` is a ``DecodeError``."""
    if isinstance(exc, jwt.InvalidSignatureError):
        return "bad_signature"
    if isinstance(exc, jwt.ExpiredSignatureError):
        return "expired"
    if isinstance(exc, jwt.ImmatureSignatureError):
        return "not_yet_valid"
    if isinstance(exc, jwt.InvalidIssuerError):
        return "wrong_issuer"
    if isinstance(exc, jwt.InvalidAudienceError):
        return "wrong_audience"
    return "malformed"
