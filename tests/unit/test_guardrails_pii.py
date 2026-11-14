"""PII, both directions."""

from __future__ import annotations

import pytest

from air_orchestrator_service.guardrails import pii
from air_orchestrator_service.guardrails.pii import StreamRedactor, mask_input, scan_output

# ── Input ─────────────────────────────────────────────────────────────────────


def test_an_email_is_masked() -> None:
    result = mask_input("Reach me at jane.doe@example.com please.")

    assert "jane.doe@example.com" not in result.masked_text
    assert "[EMAIL]" in result.masked_text
    assert "email" in result.categories


@pytest.mark.parametrize(
    "card",
    [
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "4111111111111111",
        "5500 0000 0000 0004",
        "3782 822463 10005",  # Amex, 4-6-5
    ],
)
def test_a_card_number_is_masked_on_input(card: str) -> None:
    """Spec 0007, ``REQ-PII-001``/``002``: the five formats SIT found (OR-001), none
    left half-masked by the phone rule."""
    result = mask_input(f"My card {card} was charged twice.")

    assert result.masked_text == "My card [CARD] was charged twice."
    assert result.categories == ["credit_card"]


def test_a_non_luhn_digit_run_is_not_masked_as_a_card() -> None:
    """``REQ-PII-003``: an order id this long is not a card, and is not a phone
    number either."""
    for text in ("Order 1234567812345678 is late.", "Order 1234 5678 1234 5678 is late."):
        result = mask_input(text)

        assert result.masked_text == text
        assert result.categories == []


def test_a_card_next_to_other_pii_masks_both() -> None:
    result = mask_input("Card 4111 1111 1111 1111, call me on 555 123 4567.")

    assert result.masked_text == "Card [CARD], call me on [PHONE]."
    assert result.categories == ["credit_card", "phone"]


def test_input_and_output_share_one_card_rule() -> None:
    """``REQ-PII-005``: the two directions drifted once; one pattern means they cannot."""
    assert pii._OUTPUT_PATTERNS["credit_card"] is pii._CARD_PATTERN
    text = "Card 3782 822463 10005 and order 1234567812345678."

    assert "[CARD]" in mask_input(text).masked_text
    assert "10005" not in mask_input(text).masked_text
    assert scan_output(text).categories == ["credit_card"]
    assert "1234567812345678" in scan_output(text).safe_text


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


# ── Streaming (spec 0004) ─────────────────────────────────────────────────────

#: Every output category, several spanning whitespace, with ordinary prose around
#: them so a split can land anywhere: inside a token, between tokens, in a match.
_PII_DENSE = (
    "Thanks for waiting. I've updated the record: email vikram.s@example.com, "
    "phone +91 98765 43210, card 4111 1111 1111 1111, and the order "
    "1234567890123456 stays as is. Your API key sk-abcdefghijklmnopqrstuvwx "
    "was rotated; the old link postgres://admin:hunter2@db.internal:5432/app "
    "no longer works. Anything else?"
)


def _stream(chunks: list[str]) -> tuple[list[str], str]:
    """Feed chunks, returning the cumulative released text after each one, and the
    final released text after `finish()`."""
    redactor = StreamRedactor()
    released = ""
    seen: list[str] = []
    for chunk in chunks:
        released += redactor.feed(chunk)
        seen.append(released)
    released += redactor.finish()
    return seen, released


def test_stream_redactor_never_releases_split_pii() -> None:
    """REQ-STRM-003 (AC-1): a phone number split across deltas, each half harmless alone."""
    text = "Sure — call me on 555 12" + "3 4567 tomorrow morning, " + "x" * 80 + " done."
    final = scan_output(text).safe_text
    assert "555 123 4567" not in final  # the final scan does redact it

    chunks = ["Sure — call me on 555 12", "3 4567 tomorrow morning, ", "x" * 80, " done."]
    seen, released = _stream(chunks)

    for partial in seen:
        assert "555 12" not in partial
        assert "3 4567" not in partial
        assert final.startswith(partial)
    assert released == final


def test_stream_redactor_matches_the_final_scan_under_any_chunking() -> None:
    """REQ-STRM-003: at every split point, and fed one character at a time, everything
    released so far is a prefix of the final redacted answer — so it can never contain
    what that answer masks — and the whole release equals it."""
    final = scan_output(_PII_DENSE).safe_text
    assert "4111 1111 1111 1111" not in final and "hunter2" not in final  # sanity

    for i in range(1, len(_PII_DENSE)):
        seen, released = _stream([_PII_DENSE[:i], _PII_DENSE[i:]])
        assert all(final.startswith(partial) for partial in seen), f"split at {i}"
        assert released == final, f"split at {i}"

    seen, released = _stream(list(_PII_DENSE))
    assert all(final.startswith(partial) for partial in seen)
    assert released == final


def test_stream_redactor_releases_ordinary_prose_before_the_end() -> None:
    """The point of streaming: text with nothing to hide flows, a hold-back behind."""
    words = [f"word{i} " for i in range(60)]
    redactor = StreamRedactor()

    released = "".join(redactor.feed(w) for w in words)

    assert released  # something came out mid-stream
    assert len("".join(words)) - len(released) >= 64  # but the tail was held back
