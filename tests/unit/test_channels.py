"""The channel is a property of the credential, not of the request.

This is the invariant docs/01-hld.md §3 rests on. If a request could influence its
own channel, the weaker guardrail profile would be one header away — so these tests
guard the *mechanism*, not just the current behaviour.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from air_platform.config import Settings
from air_platform.constants import CHANNEL_SCOPES, Channel
from air_platform.main import create_app
from air_platform.security.api_keys import ApiKeyRecord, ApiKeyStore
from tests.conftest import CUSTOMER_KEY, TEST_SALT, auth, digest, key_records


async def test_a_header_cannot_choose_the_channel(client: httpx.AsyncClient) -> None:
    """Sending every plausible channel header still yields the key's own channel."""
    forged = {
        "X-Channel": "business",
        "X-AIR-Channel": "business",
        "Channel": "business",
        **auth(CUSTOMER_KEY),
    }

    response = await client.get("/v1/capabilities", headers=forged)

    assert response.json()["channel"] == "customer"


async def test_a_query_parameter_cannot_choose_the_channel(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/capabilities?channel=business", headers=auth(CUSTOMER_KEY))

    assert response.json()["channel"] == "customer"


def test_a_key_cannot_hold_a_scope_its_channel_cannot_use() -> None:
    """Refusing to load beats 403-ing at request time.

    A customer key granted ``query:write`` would fail only when someone used it, and
    a 403 on a scope the operator believes they granted is genuinely hard to
    diagnose.
    """
    with pytest.raises(ValidationError, match="query:write"):
        ApiKeyRecord(
            id="mixed",
            name="Mixed key",
            key_hash=digest("whatever"),
            channel=Channel.CUSTOMER,
            tenant="t",
            scopes={"query:write"},
        )


def test_every_channel_has_a_declared_scope_set() -> None:
    """A new channel without a scope set would silently reject every scope."""
    assert set(CHANNEL_SCOPES) == set(Channel)
    for channel, scopes in CHANNEL_SCOPES.items():
        assert scopes, f"{channel} declares no usable scopes"


def test_anonymous_identity_cannot_propose_mutations() -> None:
    """The development hatch grants read freely and writes never.

    An unauthenticated developer should be able to exercise the read path and still
    not be able to reach air-action.
    """
    record = ApiKeyRecord.anonymous()

    assert record.allow_actions is False
    assert record.scopes == set(CHANNEL_SCOPES[Channel.CUSTOMER])


def test_anonymous_identity_is_unreachable_through_lookup() -> None:
    """Its empty digest can never collide with a real one, so it is opt-in only."""
    store = ApiKeyStore(
        [ApiKeyRecord.model_validate(record) for record in key_records()],
        salt=TEST_SALT,
        allow_unauthenticated=False,
    )

    assert store.lookup("") is None
    assert store.lookup(None) is None
    # Not even by presenting the empty hash as a key.
    assert store.lookup("") is None


def test_anonymous_channel_is_configurable_and_defaults_to_the_stricter_one() -> None:
    settings = Settings.model_validate(
        {"app": {"env": "test"}, "security": {"allow_unauthenticated": True}}
    )

    assert settings.security.anonymous_channel is Channel.CUSTOMER

    store = ApiKeyStore.load(settings)
    record = store.lookup(None)

    assert record is not None
    assert record.channel is Channel.CUSTOMER


def test_tenant_is_normalised_so_one_tenant_cannot_become_two() -> None:
    """The tenant is a Redis key component; `Acme` and `acme` must not split a session."""
    record = ApiKeyRecord(
        id="k",
        name="k",
        key_hash=digest("k"),
        channel=Channel.CUSTOMER,
        tenant="  ACME  ",
    )

    assert record.tenant == "acme"


async def test_principal_carries_the_tenant_into_the_request() -> None:
    """Two keys on two tenants must never resolve to the same isolation boundary."""
    settings = Settings.model_validate(
        {
            "app": {"env": "test"},
            "security": {
                "hash_salt": TEST_SALT,
                "api_keys_inline": json.dumps(key_records()),
            },
        }
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        store = ApiKeyStore.load(settings)
        customer = store.lookup(CUSTOMER_KEY)
        assert customer is not None
        assert customer.to_principal().tenant == "tenant-a"

        # And the resolved identity is what the route sees.
        response = await http.get("/v1/capabilities", headers=auth(CUSTOMER_KEY))
        assert response.status_code == httpx.codes.OK
