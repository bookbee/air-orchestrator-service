"""The review set's flags — spec 0006 (``REQ-PRM-007``).

The live run is ``make prompt-review``. This checks that the flags catch what they
name, so a clean live run means something.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import ModuleType

import pytest

_TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "prompt_review.py"


def _review() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prompt_review", _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        # A good reply: short, honest, ends on a question.
        ("Sorry it's late, that's frustrating. What's your order number?", []),
        ("I can't look orders up here, but a person can. Just ask to speak to a person.", []),
        # A question just before a closing sentence is still a next step.
        ("Could you share your order number? Once I have it, I can explain the options.", []),
        # Claims an action it cannot take (Article II, in prose).
        ("I've checked your order and it's on its way. Anything else?", ["claims_action"]),
        ("Your refund has been processed. Let me know if you need more.", ["claims_action"]),
        ("I have gone ahead and cancelled it. Anything else?", ["claims_action"]),
        # Offers an action it cannot take: "yes" would lead nowhere.
        ("Would you like me to connect you with someone?", ["offers_to_act"]),
        ("Want me to flag that for you now?", ["offers_to_act"]),
        ("I'll make sure it's handled. Anything else?", ["offers_to_act"]),
        # Promises a check it cannot make (found reviewing spec 0012; v2 does it too).
        ("Can you share your order number so I can look into what's going on?", ["promises_check"]),
        ("What's the order number, so we can see where things stand?", ["promises_check"]),
        ("Let me check that for you. What's the order number?", ["promises_check"]),
        ("What's your order number? A person can look into it for you.", []),
        # Runs long.
        ("One. Two. Three. Four. Five. Six?", ["too_long"]),
        # Dead end.
        ("Your order is late. That happens sometimes.", ["no_next_step"]),
    ],
)
def test_the_flags_catch_what_they_name(reply: str, expected: list[str]) -> None:
    assert _review().flags(reply) == expected


def test_the_review_set_loads() -> None:
    conversations = _review().load_review_set()

    names = [c["name"] for c in conversations]
    assert len(names) == len(set(names)) >= 5
    assert all(c["turns"] and c.get("expect") for c in conversations)
    # Multi-turn on purpose: the persona is judged on a conversation.
    assert any(len(c["turns"]) > 1 for c in conversations)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        # With lookups, saying it checked is true (spec 0012)...
        ("I've checked your order 48213 and it's in transit. Anything else?", []),
        ("I looked up your recent orders: 48213 is on its way. Anything else?", []),
        # ...but a claimed change is still Article II broken in prose.
        ("I've cancelled order 55120 for you. Anything else?", ["claims_action"]),
        ("Your refund has been processed. Let me know if you need more.", ["claims_action"]),
    ],
)
def test_a_lookups_conversation_flags_only_claimed_changes(reply: str, expected: list[str]) -> None:
    assert _review().flags(reply, lookups=True) == expected


def test_a_lookups_conversation_is_skipped_unless_asked_for() -> None:
    """Without lookups the rule under review never fires, so the case would pass for
    the wrong reason."""
    review = _review()
    needing = [c["name"] for c in review.load_review_set() if c.get("requires") == "lookups"]

    assert needing == ["order-status-with-lookup", "order-status-lookup-fails"]
    assert frozenset({"lookups"}) == review.REQUIREMENTS
