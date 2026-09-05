"""PII, both directions.

Patterns ported from `shopassist-service`'s `services/pii_masker.py` (input)
and `services/guardrails.py`'s `screen_output` (output) —
`~/git/iisc-genai/shopassist-service`, proven, tested regex detection. Not a
full NER model — the same trade-off shopassist's own docstrings make: a
phrase-anchored heuristic for name/address to avoid mangling ordinary
Title-Case text, full patterns for the rest. A real NER model or DLP API is a
drop-in replacement later as long as it keeps returning the same shapes below.

Input masking replaces with a `[CATEGORY]` token — the mask never has to be
undone, since the masked text is what's used and stored from here on. Output
scanning partially masks instead (`vi***y@gm**m`) — the customer is reading
their *own* data reflected back and needs to recognise it; a `[REDACTED]`
token there reads as a system error, not a privacy protection. `secret`
(API-key/connection-string shaped) has no legitimate customer-facing case, so
it is always fully redacted on output.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, Field

__all__ = ["MaskResult", "OutputVerdict", "mask_input", "scan_output"]

_EMAIL_PATTERN: Final = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_PHONE_PATTERN: Final = re.compile(r"(?<!\d)(?<!-)\+?(?:\d[-.\s]?){9,11}\d(?!\d)")
_NAME_PATTERN: Final = re.compile(
    r"\b(?i:my name is|i'?m|i am|this is)\s+([A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+){0,2})"
)
_ADDRESS_PATTERN: Final = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,3}\s+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Boulevard|Blvd|Court|Ct|Place|Pl)\b\.?",
    re.IGNORECASE,
)


def _mask_name(match: re.Match[str]) -> str:
    name = match.group(1)
    return match.group(0)[: -len(name)] + "[NAME]"


class MaskResult(BaseModel):
    masked_text: str
    categories: list[str] = Field(default_factory=list)

    @property
    def was_masked(self) -> bool:
        return bool(self.categories)


def mask_input(text: str) -> MaskResult:
    """Full-token redaction — the masked text is what gets used and stored
    from here on, so there is nothing downstream that needs the original
    back."""
    if not text:
        return MaskResult(masked_text=text)
    masked = text
    categories: list[str] = []
    for category, pattern, replace in (
        ("email", _EMAIL_PATTERN, "[EMAIL]"),
        ("phone", _PHONE_PATTERN, "[PHONE]"),
        ("address", _ADDRESS_PATTERN, "[ADDRESS]"),
    ):
        new = pattern.sub(replace, masked)
        if new != masked:
            categories.append(category)
        masked = new
    new = _NAME_PATTERN.sub(_mask_name, masked)
    if new != masked:
        categories.append("name")
    masked = new
    return MaskResult(masked_text=masked, categories=categories)


# ── Output side ──────────────────────────────────────────────────────────────


def _mask_segment(value: str, keep_start: int, keep_end: int, stars: int) -> str:
    if len(value) <= keep_start + keep_end:
        return value[0] + "*" * (len(value) - 1) if len(value) > 1 else value
    return value[:keep_start] + "*" * stars + value[-keep_end:]


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{_mask_segment(local, 2, 1, 3)}@{_mask_segment(domain, 2, 1, 2)}"


def _mask_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    national = digits[-10:] if len(digits) >= 10 else digits
    if len(national) == 10:
        return f"{national[:2]}*** ***{national[-2:]}"
    return _mask_segment(national, 2, 2, 4)


def _mask_credit_card(digits: str) -> str:
    last4 = digits[-4:]
    stars = "*" * (len(digits) - 4)
    groups = [stars[i : i + 4] for i in range(0, len(stars), 4)] + [last4]
    return " ".join(groups)


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_OUTPUT_PATTERNS: Final = {
    "email": _EMAIL_PATTERN,
    # Luhn-validated below, not here — a bare 13-16 digit regex also matches
    # order/tracking ids, which would otherwise be needlessly redacted.
    "credit_card": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "phone": _PHONE_PATTERN,
    "secret": re.compile(
        r"\b\w+://[^\s'\"]*:[^\s'\"]*@[^\s'\"]+"
        r"|(?:sk|AIza|AKIA)-?[A-Za-z0-9_-]{16,}"
        r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
    ),
}


class OutputVerdict(BaseModel):
    flagged: bool
    categories: list[str] = Field(default_factory=list)
    safe_text: str


def scan_output(text: str) -> OutputVerdict:
    """Never raises: an internal failure here fails open to the unredacted
    text rather than blocking an otherwise-good answer — the same trade-off
    `screen_input` makes, mirrored for the output side."""
    if not text:
        return OutputVerdict(flagged=False, safe_text=text)
    try:
        return _scan_output(text)
    except re.error:
        return OutputVerdict(flagged=False, safe_text=text)


def _scan_output(text: str) -> OutputVerdict:
    categories: list[str] = []

    # Credit-card-shaped runs are handled first and their span is protected
    # from every later pattern — not just redacted when valid. A 16-digit run
    # that *fails* Luhn (an order/tracking id) still has to be walled off,
    # because its own digits, taken alone, would otherwise satisfy the phone
    # pattern's shorter 9-12 digit window on the second pass. Segmenting the
    # text up front is what makes "leave it alone" actually mean "untouched
    # by anything downstream", not just "not treated as a card".
    segments: list[tuple[bool, str]] = []  # (protected, chunk)
    cursor = 0
    for match in _OUTPUT_PATTERNS["credit_card"].finditer(text):
        if match.start() > cursor:
            segments.append((False, text[cursor : match.start()]))
        digits = re.sub(r"[ -]", "", match.group(0))
        if _luhn_valid(digits):
            categories.append("credit_card")
            segments.append((True, _mask_credit_card(digits)))
        else:
            segments.append((True, match.group(0)))  # not a card — left as-is, still protected
        cursor = match.end()
    if cursor < len(text):
        segments.append((False, text[cursor:]))

    def redact_email(match: re.Match[str]) -> str:
        categories.append("email")
        return _mask_email(match.group(0))

    def redact_phone(match: re.Match[str]) -> str:
        categories.append("phone")
        return _mask_phone(match.group(0))

    rebuilt: list[str] = []
    for protected, chunk in segments:
        if protected:
            rebuilt.append(chunk)
            continue
        chunk = _OUTPUT_PATTERNS["email"].sub(redact_email, chunk)
        chunk = _OUTPUT_PATTERNS["phone"].sub(redact_phone, chunk)
        rebuilt.append(chunk)
    safe_text = "".join(rebuilt)

    if _OUTPUT_PATTERNS["secret"].search(safe_text):
        categories.append("secret")
        safe_text = _OUTPUT_PATTERNS["secret"].sub("[REDACTED_SECRET]", safe_text)

    return OutputVerdict(flagged=bool(categories), categories=categories, safe_text=safe_text)
