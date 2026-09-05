"""Input-side prompt-injection screening.

Patterns ported from `shopassist-service`'s `services/guardrails.py`
(`~/git/iisc-genai/shopassist-service`) — proven, tested regex screening
against blatant system-prompt-override attempts. Deliberately narrow and
fails open on anything ambiguous: a frustrated real customer must never be
blocked, so this catches the obvious cases only, not a general jailbreak
classifier. A real moderation/classifier upgrade is a documented follow-up
(air-classifier's zero-shot topic head is the natural fit), not this module.
"""

from __future__ import annotations

import re
from typing import Final, Literal

from pydantic import BaseModel

__all__ = ["InjectionVerdict", "screen_input"]

_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"(ignore|disregard|forget|override|bypass)\s+(all\s+)?(your\s+|the\s+)?"
        r"(previous|prior|above|earlier)?\s*(system\s+)?"
        r"(instructions|rules|guidelines|restrictions)",
        re.IGNORECASE,
    ),
    re.compile(r"you\s+are\s+now\s+(in\s+)?(developer|debug|dan|jailbreak)\s*mode", re.IGNORECASE),
    re.compile(r"do\s+anything\s+now\b", re.IGNORECASE),
    re.compile(
        r"(reveal|print|repeat|output|show)\s+(your\s+|the\s+)?"
        r"(system\s+prompt|initial\s+prompt|instructions)",
        re.IGNORECASE,
    ),
    re.compile(r"what\s+(is|are)\s+your\s+(system\s+prompt|instructions|rules)", re.IGNORECASE),
    re.compile(
        r"(act|pretend|roleplay)\s+as\s+(if\s+you\s+(are|have)\s+no\s+restrictions"
        r"|an?\s+unrestricted|an?\s+ai\s+with\s+no\s+(rules|filters?|restrictions))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(ai|assistant|bot)\s+with\s+no\s+(rules|filters?|restrictions|limits)", re.IGNORECASE
    ),
)

_Category = Literal["prompt_injection"]


class InjectionVerdict(BaseModel):
    blocked: bool
    category: _Category | None = None
    matched_text: str | None = None


def screen_input(text: str) -> InjectionVerdict:
    """Never raises: a regex that somehow throws must not block a real turn,
    so any internal failure fails open exactly like an ambiguous match does."""
    if not text:
        return InjectionVerdict(blocked=False)
    try:
        for pattern in _PATTERNS:
            match = pattern.search(text)
            if match:
                return InjectionVerdict(
                    blocked=True, category="prompt_injection", matched_text=match.group(0)
                )
    except re.error:
        return InjectionVerdict(blocked=False)
    return InjectionVerdict(blocked=False)
