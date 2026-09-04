"""Escalation: the one trigger with a real detector this milestone."""

from __future__ import annotations

from air_platform.guardrails.escalation import wants_human


def test_an_explicit_request_for_a_human_is_detected() -> None:
    assert wants_human("I want to talk to a human please") is True


def test_asking_for_a_real_person_is_detected() -> None:
    assert wants_human("can I speak with a real person") is True


def test_an_ordinary_question_is_not_detected() -> None:
    assert wants_human("where is my order?") is False


def test_empty_text_is_not_detected() -> None:
    assert wants_human("") is False


def test_the_word_human_alone_is_not_enough() -> None:
    """Narrow on purpose — a message that merely contains "human" somewhere
    (e.g. "is this written by a human?") should not trigger a handoff."""
    assert wants_human("are your replies written by a human or an AI?") is False
