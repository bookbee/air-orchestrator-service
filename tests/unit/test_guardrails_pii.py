"""PII, both directions."""

from __future__ import annotations

from air_platform.guardrails.pii import mask_input, scan_output

# ── Input ─────────────────────────────────────────────────────────────────────


def test_an_email_is_masked() -> None:
    result = mask_input("Reach me at jane.doe@example.com please.")

    assert "jane.doe@example.com" not in result.masked_text
    assert "[EMAIL]" in result.masked_text
    assert "email" in result.categories


def test_a_self_introduced_name_is_masked() -> None:
    result = mask_input("Hi, my name is Jane Doe and I have a question.")

    assert "Jane Doe" not in result.masked_text
    assert "[NAME]" in result.masked_text


def test_an_ordinary_product_name_is_not_masked_as_a_person() -> None:
    """The name pattern is anchored on a self-introduction phrase precisely
    so this does not fire."""
    result = mask_input("Is the Gaming Laptop Pro back in stock?")

    assert result.masked_text == "Is the Gaming Laptop Pro back in stock?"
    assert result.categories == []


def test_text_with_no_pii_is_unchanged() -> None:
    result = mask_input("Where is my order?")

    assert result.masked_text == "Where is my order?"
    assert result.was_masked is False


def test_empty_text_is_unchanged() -> None:
    assert mask_input("").masked_text == ""


# ── Output ────────────────────────────────────────────────────────────────────


def test_an_email_is_partially_masked_on_output() -> None:
    """Partial, not full redaction: the customer is reading their own data
    reflected back and needs to recognise it."""
    verdict = scan_output("Sure, contact jane.doe@example.com for help.")

    assert verdict.flagged is True
    assert "jane.doe@example.com" not in verdict.safe_text
    assert "@" in verdict.safe_text  # still recognisable as an email shape


def test_a_luhn_valid_card_number_is_redacted() -> None:
    verdict = scan_output("Your card 4111 1111 1111 1111 was charged.")

    assert "credit_card" in verdict.categories
    assert "4111 1111 1111 1111" not in verdict.safe_text


def test_a_luhn_invalid_digit_run_is_left_alone() -> None:
    """A 16-digit run that fails Luhn is more likely an order/tracking id
    than a card number — redacting it would be the false positive."""
    verdict = scan_output("Your tracking number is 1234 5678 9012 3456.")

    assert "credit_card" not in verdict.categories
    assert "1234 5678 9012 3456" in verdict.safe_text


def test_a_connection_string_is_fully_redacted() -> None:
    verdict = scan_output("Failed: postgres://user:hunter2@db.internal/air")

    assert "secret" in verdict.categories
    assert "hunter2" not in verdict.safe_text


def test_clean_text_is_not_flagged() -> None:
    verdict = scan_output("Your order ships tomorrow.")

    assert verdict.flagged is False
    assert verdict.safe_text == "Your order ships tomorrow."
