"""The key store: hashing, lookup, and what must never reach a log line."""

from __future__ import annotations

import json

import pytest

from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import Channel
from air_orchestrator_service.security.api_keys import (
    ANONYMOUS_KEY_ID,
    KEY_PREFIX,
    ApiKeyRecord,
    ApiKeyStore,
    generate_key,
    hash_key,
)
from tests.conftest import CUSTOMER_KEY, TEST_SALT, digest, key_records


def _store(**kw: object) -> ApiKeyStore:
    return ApiKeyStore(
        [ApiKeyRecord.model_validate(record) for record in key_records()],
        salt=TEST_SALT,
        **kw,  # type: ignore[arg-type]
    )


def test_generated_keys_are_prefixed_and_verifiable() -> None:
    raw, key_hash = generate_key(TEST_SALT)

    assert raw.startswith(KEY_PREFIX)
    assert hash_key(raw, TEST_SALT) == key_hash


def test_the_salt_is_load_bearing() -> None:
    """The same raw key under a different salt must not authenticate."""
    raw, _ = generate_key(TEST_SALT)

    assert hash_key(raw, "other-salt") != hash_key(raw, TEST_SALT)


def test_lookup_resolves_a_known_key() -> None:
    record = _store().lookup(CUSTOMER_KEY)

    assert record is not None
    assert record.id == "customer"
    assert record.channel is Channel.CUSTOMER


def test_lookup_rejects_an_unknown_key() -> None:
    assert _store().lookup("airo_not_a_real_key") is None


def test_lookup_rejects_a_disabled_key() -> None:
    """Revocation is a config edit, and must take effect without a code change."""
    store = ApiKeyStore(
        [
            ApiKeyRecord(
                id="revoked",
                name="Revoked",
                key_hash=digest(CUSTOMER_KEY),
                channel=Channel.CUSTOMER,
                tenant="t",
                enabled=False,
            )
        ],
        salt=TEST_SALT,
    )

    assert store.lookup(CUSTOMER_KEY) is None


def test_a_malformed_hash_is_dropped_rather_than_indexed() -> None:
    """Nearly always a raw key pasted where a digest belongs.

    Such a record can never match, so it is refused at load rather than discovered
    as an unexplained 401.
    """
    store = ApiKeyStore(
        [
            ApiKeyRecord(
                id="typo",
                name="Raw key pasted as a hash",
                key_hash="airo_this_is_a_raw_key_not_a_digest",
                channel=Channel.CUSTOMER,
                tenant="t",
            )
        ],
        salt=TEST_SALT,
    )

    assert len(store) == 0


def test_unauthenticated_mode_yields_the_anonymous_identity() -> None:
    record = _store(allow_unauthenticated=True).lookup(None)

    assert record is not None
    assert record.id == ANONYMOUS_KEY_ID


def test_repr_never_renders_the_digest() -> None:
    """A record reaches logs, tracebacks and debuggers; none of them may see the hash."""
    record = ApiKeyRecord(
        id="k",
        name="k",
        key_hash=digest("secret-raw-key"),
        channel=Channel.CUSTOMER,
        tenant="t",
    )

    for rendered in (repr(record), str(record)):
        assert record.key_hash not in rendered
        assert "key_hash" not in rendered


def test_parse_failure_never_echoes_the_offending_value() -> None:
    """pydantic renders the rejected input into its message, and that input is a digest."""
    settings = Settings.model_validate(
        {
            "app": {"env": "test"},
            "security": {
                "hash_salt": TEST_SALT,
                # `channel` is missing, so record 0 fails validation.
                "api_keys_inline": json.dumps(
                    [{"id": "x", "name": "x", "key_hash": digest("leak-me"), "tenant": "t"}]
                ),
            },
        }
    )

    with pytest.raises(ValueError, match="api key record 0 is invalid") as caught:
        ApiKeyStore.load(settings)

    assert digest("leak-me") not in str(caught.value)


def test_non_array_config_is_rejected_with_a_useful_message() -> None:
    settings = Settings.model_validate(
        {
            "app": {"env": "test"},
            "security": {"hash_salt": TEST_SALT, "api_keys_inline": json.dumps({"id": "x"})},
        }
    )

    with pytest.raises(ValueError, match="must be a JSON array"):
        ApiKeyStore.load(settings)


def test_tenants_lists_only_enabled_keys() -> None:
    store = ApiKeyStore(
        [
            ApiKeyRecord(
                id="live",
                name="live",
                key_hash=digest("a"),
                channel=Channel.CUSTOMER,
                tenant="alpha",
            ),
            ApiKeyRecord(
                id="dead",
                name="dead",
                key_hash=digest("b"),
                channel=Channel.CUSTOMER,
                tenant="beta",
                enabled=False,
            ),
        ],
        salt=TEST_SALT,
    )

    assert store.tenants() == frozenset({"alpha"})
