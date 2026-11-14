"""Settings: the ceilings, the production guards, and the route inventory."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from air_orchestrator_service.config import Settings
from air_orchestrator_service.constants import Channel, DownstreamService, ModelClass, Route


def _settings(**overrides: object) -> Settings:
    return Settings.model_validate({"app": {"env": "test"}, **overrides})


def test_default_port_is_this_services_slot_in_the_port_map() -> None:
    """8081 is allocated to air-orchestrator-service in air-infra/README.md.

    Asserted because the whole estate is expected to run at once on one laptop, and
    a drifted default is a collision that only shows up when everything is started.
    """
    assert _settings().app.port == 8081


def test_production_refuses_the_unauthenticated_hatch() -> None:
    with pytest.raises(ValidationError, match="allow_unauthenticated"):
        Settings.model_validate(
            {"app": {"env": "production"}, "security": {"allow_unauthenticated": True}}
        )


def test_production_refuses_raw_text_logging() -> None:
    with pytest.raises(ValidationError, match="log_raw_text"):
        Settings.model_validate({"app": {"env": "production"}, "security": {"log_raw_text": True}})


def test_development_allows_both_hatches() -> None:
    """They exist to be used locally; only production forbids them."""
    settings = Settings.model_validate(
        {
            "app": {"env": "development"},
            "security": {"allow_unauthenticated": True, "log_raw_text": True},
        }
    )

    assert settings.security.allow_unauthenticated is True


def test_redis_session_backend_requires_a_url() -> None:
    """Falling back to in-process memory is the failure that looks like it works.

    Every replica answers, and a conversation simply forgets itself whenever the
    load balancer moves — so this is a startup error rather than a runtime surprise.
    """
    with pytest.raises(ValidationError, match=re.escape("session.redis_url")):
        Settings.model_validate(
            {"app": {"env": "test"}, "session": {"backend": "redis", "redis_url": ""}}
        )


def test_disabled_cache_does_not_require_a_redis_url() -> None:
    """The guard must not fire for a backend that is configured but switched off."""
    settings = Settings.model_validate(
        {"app": {"env": "test"}, "cache": {"enabled": False, "backend": "redis", "redis_url": ""}}
    )

    assert settings.cache.enabled is False


# ── Downstream inventory ──────────────────────────────────────────────────────


def test_every_optional_service_defaults_to_disabled() -> None:
    """Not every deployment runs every optional service.

    Defaulting them on would make a fresh checkout report itself degraded against
    services that do not exist, which trains an operator to ignore the signal.
    """
    downstream = _settings().downstream

    for service in (
        DownstreamService.CLASSIFIER,
        DownstreamService.RAG,
        DownstreamService.TOOLS_GATEWAY,
        DownstreamService.RECOMMENDER,
    ):
        config = downstream.for_service(service)
        assert config is not None
        assert config.enabled is False
        assert config.configured is False


def test_air_infra_is_not_in_the_optional_group() -> None:
    """It has no `enabled` flag: it is either reachable or the service is unready."""
    assert _settings().downstream.for_service(DownstreamService.INFRA) is None


def test_the_model_gateway_is_not_in_the_optional_group() -> None:
    """Same reasoning as air-infra: the gateway is mandatory, not one of the optional five."""
    assert _settings().downstream.for_service(DownstreamService.MODEL_GATEWAY) is None


def test_direct_is_always_available() -> None:
    """The model gateway alone can answer a direct turn, so the route never disappears."""
    assert _settings().downstream.routes() == frozenset({Route.DIRECT})


def test_air_rag_defaults_to_its_port_map_slot() -> None:
    """8083 belongs to air-model-gateway; air-rag-service was bumped to 8087
    (air-infra/README.md)."""
    assert _settings().downstream.rag.base_url == "http://localhost:8087"


# ── air-model-gateway (spec 0003) ─────────────────────────────────────────────


def test_model_gateway_defaults_to_its_port_map_slot() -> None:
    assert _settings().model_gateway.base_url == "http://localhost:8083"


def test_model_gateway_is_unconfigured_without_an_api_key() -> None:
    """Same shape as air-infra: configured means an api_key is set, nothing else."""
    assert _settings().model_gateway.configured is False
    assert _settings(model_gateway={"api_key": "secret"}).model_gateway.configured is True


def test_model_class_defaults_to_cloud() -> None:
    """REQ-MGW-003: `cloud` is the class the gateway owner assigned this service when
    the `generative` alias was retired (air-model-gateway docs/06-consumer-migration.md)."""
    assert _settings().model_gateway.model_class is ModelClass.CLOUD
    assert _settings(model_gateway={"model_class": "local"}).model_gateway.model_class is (
        ModelClass.LOCAL
    )


@pytest.mark.parametrize("value", ["generative", "claude-sonnet-5", "anthropic:claude-sonnet-5"])
def test_a_retired_model_alias_fails_validation(value: str) -> None:
    """REQ-MGW-004: an alias or a model name is a 422 on every call at the gateway, so it
    must be a boot failure here — the previous contract break hid behind a green probe."""
    with pytest.raises(ValidationError, match="model_class"):
        _settings(model_gateway={"model_class": value})


def test_legacy_llm_settings_fail_validation_naming_the_replacement() -> None:
    """REQ-MGW-005: silently ignoring `AIR_ORCHESTRATOR_SERVICE__LLM__*` would boot with
    no token; accepting it as an alias would give one secret two names."""
    with pytest.raises(ValidationError, match="MODEL_GATEWAY"):
        _settings(llm={"api_key": "old-token", "base_url": "http://air-llm:8083"})


def test_a_settings_error_never_renders_a_secret() -> None:
    """REQ-MGW-017: the error is printed at boot into the pod log; pydantic would otherwise
    echo the input verbatim, and the input is where every secret lives."""
    with pytest.raises(ValidationError) as caught:
        _settings(
            llm={"api_key": "sk-old-gateway-token"},
            security={"hash_salt": "salt-that-must-not-leak"},
        )
    rendered = str(caught.value)
    assert "sk-old-gateway-token" not in rendered
    assert "salt-that-must-not-leak" not in rendered


def test_enabling_a_service_adds_exactly_its_route() -> None:
    settings = _settings(
        downstream={
            "rag": {"enabled": True, "base_url": "http://rag:8087"},
            "recommender": {"enabled": True, "base_url": "http://recommender:8086"},
        }
    )

    assert settings.downstream.routes() == frozenset({Route.DIRECT, Route.RAG, Route.RECOMMEND})


def test_the_tools_gateway_backs_both_the_tools_and_the_action_route() -> None:
    """One service, two routes (spec 0012). Which one a caller is offered depends on
    the gateway's live inventory for its channel; configuration admits both."""
    settings = _settings(
        downstream={"tools_gateway": {"enabled": True, "base_url": "http://gw:8084"}}
    )

    assert settings.downstream.routes() == frozenset({Route.DIRECT, Route.TOOLS, Route.ACTION})


def test_a_service_enabled_without_a_url_is_not_configured() -> None:
    """Both halves are required, so a half-configured service stays absent.

    An enabled-but-urlless client would fail on first use; treating it as absent
    keeps the failure at configuration time where it belongs.
    """
    settings = _settings(downstream={"rag": {"enabled": True, "base_url": ""}})

    assert settings.downstream.rag.configured is False
    assert Route.RAG not in settings.downstream.routes()


# ── Guardrail profiles ────────────────────────────────────────────────────────


def test_business_channel_validates_schema_by_default() -> None:
    """Structured output is the business channel's contract (review item 08)."""
    guardrails = _settings().guardrails

    assert guardrails.for_channel(Channel.BUSINESS).validate_schema is True
    assert guardrails.for_channel(Channel.CUSTOMER).validate_schema is False


def test_pii_redaction_is_on_for_both_channels_by_default() -> None:
    """Configurable on the business channel, but never absent by default."""
    guardrails = _settings().guardrails

    assert guardrails.for_channel(Channel.CUSTOMER).redact_pii is True
    assert guardrails.for_channel(Channel.BUSINESS).redact_pii is True


def test_injection_defence_is_unconditional_by_default() -> None:
    guardrails = _settings().guardrails

    for channel in Channel:
        assert guardrails.for_channel(channel).injection is True


# ── Ceilings ──────────────────────────────────────────────────────────────────


def test_turn_ceilings_have_defaults_and_bounds() -> None:
    turn = _settings().turn

    assert turn.deadline_ms == 15_000
    assert turn.max_cost_usd == 0.25
    assert turn.max_model_calls == 4

    with pytest.raises(ValidationError):
        Settings.model_validate({"app": {"env": "test"}, "turn": {"max_cost_usd": 0}})


def test_semantic_cache_threshold_is_conservative_by_default() -> None:
    """ "Where is order 123" and "where is order 456" are near-identical vectors.

    A permissive threshold turns the cost win into a wrong-answer incident, so the
    default errs high (docs/00-plan.md §4 Q5).
    """
    assert _settings().cache.similarity_threshold >= 0.95
    assert _settings().cache.enabled is False


def test_proposal_ttl_is_short_and_bounded() -> None:
    """A stale "yes" must not be able to execute an hour-old proposal."""
    session = _settings().session

    assert session.proposal_ttl_seconds == 300
    with pytest.raises(ValidationError):
        Settings.model_validate({"app": {"env": "test"}, "session": {"proposal_ttl_seconds": 7200}})


def test_session_cost_ceiling_is_unlimited_by_default() -> None:
    """Opt-in: a deployment that never sets this sees no behaviour change."""
    assert _settings().session.max_cost_usd is None


def test_session_cost_ceiling_can_be_configured() -> None:
    settings = _settings(session={"max_cost_usd": 2.5})

    assert settings.session.max_cost_usd == 2.5


def test_competitor_names_default_to_empty() -> None:
    """No fixed vertical to hardcode names for — a deployment's own config."""
    assert _settings().guardrails.competitor_names == []


# ── air-tools-gateway (spec 0012) ─────────────────────────────────────────────


@pytest.mark.parametrize("section", ["tools", "action"])
def test_a_disabled_retired_section_is_accepted_and_reported(section: str) -> None:
    """Every ``.env`` an older ``make env`` wrote sets these with ``ENABLED=false``."""
    settings = _settings(downstream={section: {"enabled": "false", "base_url": "x"}})

    assert settings.downstream.retired_sections() == [section]
    assert settings.downstream.tools_gateway.configured is False


@pytest.mark.parametrize("section", ["tools", "action"])
def test_an_enabled_retired_section_refuses_startup_naming_the_replacement(section: str) -> None:
    with pytest.raises(ValidationError, match="DOWNSTREAM__TOOLS_GATEWAY__"):
        _settings(downstream={section: {"enabled": "true", "base_url": "http://x:8084"}})


def test_no_retired_section_by_default() -> None:
    assert _settings().downstream.retired_sections() == []


def test_tools_gateway_defaults_are_its_port_slot_and_an_empty_allow_list() -> None:
    gateway = _settings().downstream.tools_gateway

    assert gateway.base_url == "http://localhost:8084"
    assert gateway.allow.customer == []
    assert gateway.allow.business == []
    assert (gateway.inventory_ttl_s, gateway.inventory_grace_s) == (60, 300)


def test_the_inventory_grace_must_cover_the_ttl() -> None:
    with pytest.raises(ValidationError, match="inventory_grace_s"):
        _settings(downstream={"tools_gateway": {"inventory_ttl_s": 120, "inventory_grace_s": 60}})


@pytest.mark.parametrize("entry", ["orders", "Orders.get", "orders.*.get", "*", "orders.", ""])
def test_an_allow_list_entry_must_be_a_name_or_a_namespace_pattern(entry: str) -> None:
    with pytest.raises(ValidationError, match="namespace"):
        _settings(downstream={"tools_gateway": {"allow": {"customer": [entry]}}})


def test_an_allow_list_pattern_admits_its_namespace_only() -> None:
    allow = _settings(
        downstream={"tools_gateway": {"allow": {"customer": ["orders.*", "store.hours"]}}}
    ).downstream.tools_gateway.allow

    assert allow.admits(Channel.CUSTOMER, "orders.list_recent")
    assert allow.admits(Channel.CUSTOMER, "orders.returns.start")
    assert allow.admits(Channel.CUSTOMER, "store.hours")
    assert not allow.admits(Channel.CUSTOMER, "ordersx.get")
    assert not allow.admits(Channel.CUSTOMER, "store.hours_today")
    assert not allow.admits(Channel.BUSINESS, "orders.list_recent")


def test_gateway_keys_are_per_tenant_with_a_single_tenant_fallback() -> None:
    per_tenant = _settings(
        downstream={
            "tools_gateway": {
                "api_key": "fallback-read",
                "execute_key": "fallback-exec",
                "tenant_keys": {"brightcart": {"read": "r1", "execute": "x1"}},
            }
        }
    ).downstream.tools_gateway
    single = _settings(
        downstream={"tools_gateway": {"api_key": "r", "execute_key": "x"}}
    ).downstream.tools_gateway
    half = _settings(downstream={"tools_gateway": {"api_key": "r"}}).downstream.tools_gateway

    pair = per_tenant.keys_for("brightcart")
    assert pair is not None
    assert (pair.read.get_secret_value(), pair.execute.get_secret_value()) == ("r1", "x1")
    # A map is set, so a tenant missing from it gets nothing, not the fallback.
    assert per_tenant.keys_for("othershop") is None
    assert single.serves("anyone")
    # Both halves are needed: a read key alone could never execute a confirmed change.
    assert not half.serves("anyone")


def test_gateway_keys_never_render() -> None:
    gateway = _settings(
        downstream={
            "tools_gateway": {
                "tenant_keys": {"brightcart": {"read": "r-secret", "execute": "x-secret"}}
            }
        }
    ).downstream.tools_gateway

    rendered = repr(gateway) + str(gateway.model_dump())
    assert "r-secret" not in rendered
    assert "x-secret" not in rendered
