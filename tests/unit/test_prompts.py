"""The prompt registry."""

from __future__ import annotations

import pytest

from air_orchestrator_service.config import Settings
from air_orchestrator_service.prompts.registry import PromptNotFoundError, PromptRegistry


def test_the_direct_route_resolves_to_a_built_in_prompt() -> None:
    registry = PromptRegistry(Settings.model_validate({"app": {"env": "test"}}))

    prompt = registry.get("direct")

    assert prompt.version == "v1"
    assert prompt.system


def test_a_pin_selects_a_specific_version() -> None:
    registry = PromptRegistry(
        Settings.model_validate({"app": {"env": "test"}, "prompts": {"pins": {"direct": "v1"}}})
    )

    assert registry.get("direct").version == "v1"


def test_an_unknown_route_raises() -> None:
    registry = PromptRegistry(Settings.model_validate({"app": {"env": "test"}}))

    with pytest.raises(PromptNotFoundError):
        registry.get("no-such-route")


def test_a_pin_to_a_version_that_does_not_exist_raises() -> None:
    registry = PromptRegistry(
        Settings.model_validate({"app": {"env": "test"}, "prompts": {"pins": {"direct": "v99"}}})
    )

    with pytest.raises(PromptNotFoundError):
        registry.get("direct")
