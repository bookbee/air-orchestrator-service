"""Keys and tokens for the customer token tests — spec 0014.

Key pairs are generated once per test session with ``cryptography``: brightcart's
signing pair, an unrelated pair (for a bad signature), and a P-384 pair (refused for
``ES256``). ``mint`` builds tokens with PyJWT; the forged ones it cannot make (an
``HS256`` token keyed with the public key's bytes, ``alg: none``) are built by hand.
Times are relative to ``time.time()`` with margins far wider than the 30 s leeway.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from functools import cache
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from air_orchestrator_service.security.customer_token import AUDIENCE, TOKEN_TYPE, CustomerTokenKey

ISSUER = "air-web-console-dev"
TENANT = "tenant-a"
KID = "brightcart-dev-1"
SUBJECT = "cust_1001"


@cache
def private_key(name: str = "signer", curve: str = "P-256") -> ec.EllipticCurvePrivateKey:
    del name  # one cached pair per name
    return ec.generate_private_key(ec.SECP256R1() if curve == "P-256" else ec.SECP384R1())


def public_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def private_pem(key: ec.EllipticCurvePrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def key_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "issuer": ISSUER,
        "tenant": TENANT,
        "kid": KID,
        "alg": "ES256",
        "public_key": public_pem(private_key()),
    }
    record.update(overrides)
    return record


def configured_key(**overrides: Any) -> CustomerTokenKey:
    return CustomerTokenKey.model_validate(key_record(**overrides))


def claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    body: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUBJECT,
        "iat": now,
        "exp": now + 600,
        "jti": "jti-1",
        "air_tenant": TENANT,
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None}


def mint(
    *,
    signer: ec.EllipticCurvePrivateKey | None = None,
    header: dict[str, Any] | None = None,
    **overrides: Any,
) -> str:
    fields = {"kid": KID, "typ": TOKEN_TYPE, **(header or {})}
    return jwt.encode(
        claims(**overrides),
        signer or private_key(),
        algorithm="ES256",
        headers={k: v for k, v in fields.items() if v is not None},
    )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forged(alg: str) -> str:
    """``HS256`` keyed with the configured public key's PEM bytes, or ``none``."""
    head = _b64(json.dumps({"alg": alg, "kid": KID, "typ": TOKEN_TYPE}).encode())
    body = _b64(json.dumps(claims()).encode())
    signing_input = f"{head}.{body}".encode()
    if alg == "none":
        return f"{head}.{body}."
    secret = public_pem(private_key()).encode()
    return f"{head}.{body}.{_b64(hmac.new(secret, signing_input, hashlib.sha256).digest())}"
