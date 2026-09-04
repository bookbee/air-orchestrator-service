"""Escalation: the one trigger that's real today.

`constants.EscalationReason` names four triggers; only `EXPLICIT_REQUEST` has
a detector here. The other three depend on capabilities this service doesn't
have yet (classification confidence, retrieval confidence, tool-call
tracking are all Phase 3) — reserved in the enum, not faked here.

For this milestone, escalation is a stubbed handoff: record it, and tell the
customer honestly. There is no support-desk integration to hand off to yet
(`engine/turn.py` generates a reference and logs a structured record — that
record is what a real integration would forward).
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["wants_human"]

_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"\b(talk|speak)\s+(to|with)\s+a\s+(human|person|agent|representative)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bhuman\s+(support|agent|help)\b", re.IGNORECASE),
    re.compile(r"\breal\s+person\b", re.IGNORECASE),
    re.compile(r"\bcustomer\s+service\s+(rep|representative|agent)\b", re.IGNORECASE),
    re.compile(r"\bescalate\s+(this|my\s+(issue|case|request))\b", re.IGNORECASE),
)


def wants_human(text: str) -> bool:
    """Never raises: fails open to *not* escalating, the same discipline
    every other guardrail here uses — an over-eager escalation is a worse
    customer experience than a missed one, not a safety issue."""
    if not text:
        return False
    try:
        return any(pattern.search(text) for pattern in _PATTERNS)
    except re.error:
        return False
