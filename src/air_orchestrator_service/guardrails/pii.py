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

__all__ = ["MaskResult", "OutputVerdict", "StreamRedactor", "mask_input", "scan_output"]

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


# ── Streaming output (spec 0004) ─────────────────────────────────────────────

#: Characters of the newest text never released mid-stream. Every output pattern
#: that can span whitespace (phone, card) is far shorter than this, so one still
#: being typed out is always inside the held-back tail rather than half-sent.
STREAM_HOLDBACK: Final[int] = 64

_WHITESPACE: Final = re.compile(r"\s")


class StreamRedactor:
    """The output guardrail, for text that arrives in pieces.

    :func:`scan_output` needs the whole answer: a phone number or a card split
    across two model deltas is not recognisable in either, so scanning each delta
    alone would stream exactly the PII it exists to mask. This class releases only
    a prefix that can no longer change under a later scan:

    * it ends at whitespace, so no single-token pattern (email, key, JWT, URL with
      credentials — all unbounded) is ever cut mid-token;
    * it stays :data:`STREAM_HOLDBACK` characters behind the newest text, so no
      whitespace-spanning pattern (phone, card) is released before it completes;
    * no output-pattern match in the text so far straddles it.

    It releases ``scan_output(prefix).safe_text`` minus what it already released.
    If that would ever not extend the text already sent, it stops releasing for the
    rest of the stream — over-holding is safe, because the caller's final answer
    is the full :func:`scan_output` of the whole text either way.
    """

    __slots__ = ("_broken", "_raw", "_released", "_released_raw")

    def __init__(self) -> None:
        self._raw = ""
        self._released = ""
        self._released_raw = 0
        self._broken = False

    def feed(self, chunk: str) -> str:
        """Add model text; return what is now safe to send (possibly empty)."""
        self._raw += chunk
        return self._release(self._safe_cut())

    def finish(self) -> str:
        """The stream ended normally: release the rest, fully scanned."""
        return self._release(len(self._raw))

    @property
    def text(self) -> str:
        """Everything the model sent, unredacted — for the final scan, never output."""
        return self._raw

    def _safe_cut(self) -> int:
        limit = len(self._raw) - STREAM_HOLDBACK
        if limit <= self._released_raw:
            return self._released_raw
        boundary = -1
        for match in _WHITESPACE.finditer(self._raw, self._released_raw, limit):
            boundary = match.end()
        if boundary <= self._released_raw:
            return self._released_raw
        # Pull the cut back to the start of any match that straddles it, until none
        # does. Card-shaped runs count whether or not they pass Luhn: the final scan
        # walls them off either way, so a prefix must not split one.
        cut = boundary
        moved = True
        while moved:
            moved = False
            for pattern in _OUTPUT_PATTERNS.values():
                for match in pattern.finditer(self._raw):
                    if match.start() < cut < match.end():
                        cut = match.start()
                        moved = True
        return max(cut, self._released_raw)

    def _release(self, cut: int) -> str:
        # Nothing new to release — unless this is `finish()` reaching the end.
        if self._broken or (cut <= self._released_raw and cut < len(self._raw)):
            return ""
        safe = scan_output(self._raw[:cut]).safe_text
        if not safe.startswith(self._released):
            # A later scan disagreed with what was already sent. Should not happen
            # given the cut rules; if it does, stop rather than send a correction.
            self._broken = True
            return ""
        out = safe[len(self._released) :]
        self._released = safe
        self._released_raw = cut
        return out
