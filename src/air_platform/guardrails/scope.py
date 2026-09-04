"""The scope guard: what air-platform will not engage with, decided now
rather than retrofitted under launch pressure.

Same shape as `injection.py` — pattern-based, deliberately narrow, fails
open on anything ambiguous so a legitimate customer is never caught by a
pattern meant for the obvious case. Three categories are checkable this way;
a fourth is a documented decision rather than a detector:

* **Abuse** — profanity/hostility aimed at the assistant or the business.
* **Discount negotiation** — asking for a price change, a coupon, or to match
  a competitor's price. Not a security concern like injection; a business
  boundary, so the response is a clear redirect, not a bare refusal.
* **Competitor mention** — naming a configured competitor
  (`GuardrailSettings.competitor_names`, empty by default: this service has
  no fixed vertical to hardcode names for).
* **Chit-chat is deliberately not a category here.** Reliably classifying
  "is this on-topic" by regex is not feasible at this layer, and a false
  positive here costs a real customer their answer. The decision is to let
  `prompts/registry.py`'s own instructions govern it — answer plainly, in
  scope, invent nothing — rather than build a detector that would mostly be
  wrong.
"""

from __future__ import annotations

import re
from typing import Final, Literal

from pydantic import BaseModel

__all__ = ["ScopeVerdict", "screen_scope"]

_Category = Literal["abuse", "discount_negotiation", "competitor_mention"]

_ABUSE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\b(fuck|shit|asshole|bastard|bitch)\b", re.IGNORECASE),
    re.compile(
        r"\byou'?re\s+(useless|worthless|garbage|trash|stupid|an?\s+idiot)\b", re.IGNORECASE
    ),
)

_DISCOUNT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\b(give|offer)\s+me\s+a\s+discount\b", re.IGNORECASE),
    re.compile(r"\b(coupon|promo)\s*code\b", re.IGNORECASE),
    re.compile(r"\bcan\s+you\s+(lower|reduce|drop)\s+the\s+price\b", re.IGNORECASE),
    re.compile(r"\bmatch\s+(this\s+|that\s+)?(price|competitor)\b", re.IGNORECASE),
    re.compile(r"\bnegotiate\s+(the\s+)?(price|cost)\b", re.IGNORECASE),
)

_RESPONSES: Final[dict[_Category, str]] = {
    "abuse": (
        "I want to help, but I need the conversation to stay respectful to keep going. "
        "Rephrase your question and I'll do my best to answer it."
    ),
    "discount_negotiation": (
        "I'm not able to offer discounts or change pricing — that's outside what I can do here. "
        "I can answer questions about your order or our policies."
    ),
    "competitor_mention": (
        "I can only speak to our own products, policies and orders — I'm not able to compare "
        "us to other companies. Happy to help with anything on our side."
    ),
}


class ScopeVerdict(BaseModel):
    blocked: bool
    category: _Category | None = None
    matched_text: str | None = None
    #: The response to send instead of running the normal pipeline — fixed
    #: per category (see the module docstring on why this differs from
    #: `injection.py`'s bare refusal).
    response: str | None = None


def screen_scope(text: str, *, competitor_names: list[str]) -> ScopeVerdict:
    """Never raises: the same fail-open discipline `screen_input` uses."""
    if not text:
        return ScopeVerdict(blocked=False)
    try:
        for pattern in _ABUSE_PATTERNS:
            match = pattern.search(text)
            if match:
                return ScopeVerdict(
                    blocked=True,
                    category="abuse",
                    matched_text=match.group(0),
                    response=_RESPONSES["abuse"],
                )
        for pattern in _DISCOUNT_PATTERNS:
            match = pattern.search(text)
            if match:
                return ScopeVerdict(
                    blocked=True,
                    category="discount_negotiation",
                    matched_text=match.group(0),
                    response=_RESPONSES["discount_negotiation"],
                )
        for name in competitor_names:
            if name and name.lower() in text.lower():
                return ScopeVerdict(
                    blocked=True,
                    category="competitor_mention",
                    matched_text=name,
                    response=_RESPONSES["competitor_mention"],
                )
    except re.error:
        return ScopeVerdict(blocked=False)
    return ScopeVerdict(blocked=False)
