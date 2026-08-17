"""Settings: the ceilings, the production guards, and the route inventory."""

from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from air_platform.config import Settings
from air_platform.constants import Channel, DownstreamService, Route


def _settings(**overrides: object) -> Settings:
    return Settings.model_validate({"app": {"env": "test"}, **overrides})


def test_default_port_is_this_services_slot_in_the_port_map() -> None:
    """8081 is allocated to air-platform in air-infra/README.md.

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
    """air-rag, air-tools and air-action are empty repos.

    Defaulting them on would make a fresh checkout report itself degraded against
    services that do not exist, which trains an operator to ignore the signal.
    """
    downstream = _settings().downstream

    for service in (
        DownstreamService.CLASSIFIER,
        DownstreamService.RAG,
        DownstreamService.TOOLS,
        DownstreamService.ACTION,
        DownstreamService.RECOMMENDER,
    ):
        config = downstream.for_service(service)
        assert config is not None
        assert config.enabled is False
        assert config.configured is False


def test_air_infra_is_not_in_the_optional_group() -> None:
    """It has no `enabled` flag: it is either reachable or the service is unready."""
    assert _settings().downstream.for_service(DownstreamService.INFRA) is None


def test_direct_is_always_available() -> None:
    """air-infra alone can answer a direct turn, so the route never disappears."""
    assert _settings().downstream.routes() == frozenset({Route.DIRECT})


def test_enabling_a_service_adds_exactly_its_route() -> None:
    settings = _settings(
        downstream={
            "rag": {"enabled": True, "base_url": "http://rag:8083"},
            "action": {"enabled": True, "base_url": "http://action:8085"},
        }
    )

    assert settings.downstream.routes() == frozenset({Route.DIRECT, Route.RAG, Route.ACTION})


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
    """"Where is order 123" and "where is order 456" are near-identical vectors.

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
