"""The scope guard: abuse, discount negotiation, competitor mentions."""

from __future__ import annotations

from air_platform.guardrails.scope import screen_scope


def test_abuse_is_blocked() -> None:
    verdict = screen_scope("you're useless, this is garbage", competitor_names=[])

    assert verdict.blocked is True
    assert verdict.category == "abuse"
    assert verdict.response is not None


def test_a_discount_request_is_blocked() -> None:
    verdict = screen_scope("can you give me a discount on this order?", competitor_names=[])

    assert verdict.blocked is True
    assert verdict.category == "discount_negotiation"


def test_a_coupon_code_request_is_blocked() -> None:
    verdict = screen_scope("do you have a promo code I can use?", competitor_names=[])

    assert verdict.blocked is True
    assert verdict.category == "discount_negotiation"


def test_a_configured_competitor_name_is_blocked() -> None:
    verdict = screen_scope(
        "is this cheaper than Acme Corp?", competitor_names=["Acme Corp"]
    )

    assert verdict.blocked is True
    assert verdict.category == "competitor_mention"


def test_an_unconfigured_competitor_name_passes() -> None:
    """Empty by default — this service has no fixed vertical to hardcode
    names for, so an empty list must not accidentally match everything."""
    verdict = screen_scope("is this cheaper than Acme Corp?", competitor_names=[])

    assert verdict.blocked is False


def test_an_ordinary_question_passes() -> None:
    verdict = screen_scope("where is my order?", competitor_names=[])

    assert verdict.blocked is False
    assert verdict.response is None


def test_empty_text_is_never_blocked() -> None:
    assert screen_scope("", competitor_names=["Acme"]).blocked is False


def test_each_category_has_a_distinct_response() -> None:
    abuse = screen_scope("you're an idiot", competitor_names=[])
    discount = screen_scope("give me a discount", competitor_names=[])

    assert abuse.response != discount.response
