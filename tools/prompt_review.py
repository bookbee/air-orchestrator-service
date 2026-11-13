#!/usr/bin/env python3
"""Review the customer_care prompt against a live service — spec 0006, ``REQ-PRM-007``.

Drives each conversation in ``evals/prompts/customer_care.json`` through a running
service's ``POST /v1/chat``. The session id is carried between turns, so the persona is
judged on a conversation, not on isolated replies. It prints every transcript and
flags replies that break a rule a machine *can* check:

* ``claims_action``: the reply says it has done something it cannot do ("I've checked
  your order", "your refund has been processed"). That is Article II's rule, broken
  in prose.
* ``offers_to_act``: the reply offers to do something it cannot ("Would you like me to
  connect you?"). A customer who answers "yes" gets nothing, because escalation fires only
  when they ask for a person themselves (``guardrails.escalation``).
* ``too_long``: more than five sentences or list lines. The prompt asks for two to four.
* ``no_next_step``: neither of the reply's last two sentences is a question or an offer.
  Two, not one: "Could you share your order number? Once I have it, I can help." ends on
  a next step in substance.

The flags are a floor, not a verdict on tone. The transcripts are for a person to read
against each conversation's ``expect``. Exits 1 if any reply is flagged, so a run before
and after a prompt version change can be compared.

Not part of ``make check``: it needs a running service, a gateway and real model calls,
which cost money. Run it with ``make prompt-review`` (``BASE_URL`` / ``KEY`` to override).

Usage: ``prompt_review.py [--base-url URL] [--key KEY] [--only NAME]``
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
REVIEW_SET = ROOT / "evals" / "prompts" / "customer_care.json"
MAX_SENTENCES = 5

_CLAIMS_ACTION = re.compile(
    r"\bI(?:'ve| have| just)?\s+(?:gone ahead and\s+)?"
    r"(?:checked|looked (?:up|into)|located|found your|cancell?ed|refunded|processed|"
    r"updated|changed|arranged|escalated|raised|submitted|booked|rebooked|issued|"
    r"initiated|requested|logged|flagged|reported)\b"
    r"|\b(?:has|have) been (?:cancell?ed|refunded|processed|escalated|arranged|issued|"
    r"submitted|rebooked|updated)\b",
    re.IGNORECASE,
)
_OFFERS_TO_ACT = re.compile(
    r"\b(?:would you like|do you want|want|shall|should|can|may) (?:me|I) to\s+"
    r"(?:connect|transfer|flag|escalate|pass|point|put|get|send|forward|raise|log|arrange|"
    r"check|look|cancel|refund|process|book|open)\b"
    r"|\bI(?:'ll| will| can)\s+(?:connect|transfer|flag|escalate|pass|forward|raise|log|"
    r"arrange|make sure)\b",
    re.IGNORECASE,
)
_OFFER = re.compile(
    r"let me know|happy to help|anything else|if you(?:'d| would) like|you can (?:ask|reply|"
    r"share|send|tell)|feel free|just (?:ask|reply|say)|I can help|here to help|"
    r"ask to speak to a person",
    re.IGNORECASE,
)
_SEGMENTS = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(reply: str) -> list[str]:
    """Sentences and list lines, the units ``too_long`` counts."""
    return [part.strip() for part in _SEGMENTS.split(reply.strip()) if part.strip()]


def flags(reply: str) -> list[str]:
    """The rules this reply breaks, by name; empty when it breaks none."""
    found = []
    if _CLAIMS_ACTION.search(reply):
        found.append("claims_action")
    if _OFFERS_TO_ACT.search(reply):
        found.append("offers_to_act")
    parts = sentences(reply)
    if len(parts) > MAX_SENTENCES:
        found.append("too_long")
    if not any(part.endswith("?") or _OFFER.search(part) for part in parts[-2:]):
        found.append("no_next_step")
    return found


def load_review_set(path: pathlib.Path = REVIEW_SET) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    conversations: list[dict[str, Any]] = data["conversations"]
    for conversation in conversations:
        if not conversation.get("name") or not conversation.get("turns"):
            raise ValueError(f"conversation without a name or turns: {conversation!r}")
    return conversations


def run(base_url: str, key: str, only: str | None) -> int:
    flagged = 0
    with httpx.Client(base_url=base_url, timeout=90.0) as http:
        for conversation in load_review_set():
            if only and conversation["name"] != only:
                continue
            print(f"\n=== {conversation['name']}")
            print(f"    expect: {conversation.get('expect', '-')}")
            session_id: str | None = None
            for message in conversation["turns"]:
                body: dict[str, Any] = {"message": message}
                if session_id:
                    body["session_id"] = session_id
                response = http.post("/v1/chat", headers={"X-API-Key": key}, json=body)
                if response.status_code != httpx.codes.OK:
                    print(f"  customer: {message}\n  !! HTTP {response.status_code}")
                    flagged += 1
                    break
                turn = response.json()
                session_id = turn["session_id"]
                found = flags(turn["answer"])
                flagged += bool(found)
                marker = f"  [{', '.join(found)}]" if found else ""
                print(f"  customer: {message}")
                print(f"  agent ({turn['status']}){marker}:")
                for line in turn["answer"].splitlines() or [""]:
                    print(f"    {line}")
    print(f"\n{flagged} flagged repl{'y' if flagged == 1 else 'ies'}")
    return 1 if flagged else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--key", default="airo_local_customer_key")
    parser.add_argument("--only", help="run one conversation by name")
    args = parser.parse_args()
    return run(args.base_url, args.key, args.only)


if __name__ == "__main__":
    sys.exit(main())
