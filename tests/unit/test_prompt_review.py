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
