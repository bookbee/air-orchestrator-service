#!/usr/bin/env python3
"""Review the customer_care prompt against a live service — spec 0006, ``REQ-PRM-007``.

Drives each conversation in ``evals/prompts/customer_care.json`` through a running
service's ``POST /v1/chat``. The session id is carried between turns, so the persona is
judged on a conversation, not on isolated replies. It prints every transcript and
flags replies that break a rule a machine *can* check:

* ``claims_action``: the reply says it has done something it cannot do ("I've checked
  your order", "your refund has been processed"). That is Article II's rule, broken
  in prose. In a conversation marked ``"requires": "lookups"`` the turn really has
  looked things up (spec 0012), so there only a claimed *change* is flagged.
* ``offers_to_act``: the reply offers to do something it cannot ("Would you like me to
  connect you?"). A customer who answers "yes" gets nothing, because escalation fires only
  when they ask for a person themselves (``guardrails.escalation``).
* ``promises_check``: the reply asks for a detail "so I can look into it" / "so I can
  check". The turn cannot look anything up later; only lookups that come with a message
  exist (spec 0012). Added while reviewing spec 0012, after the other flags passed it;
  ``customer_care:v2`` does it too. English only: a reviewer reads the other languages.
* ``states_figure``: the reply gives a figure with a time unit ("5 to 10 working days").
  Not wrong in itself: a cited source, a lookup or a proposal may have given it. The
  runner sees replies, not what the turn was given, so a reviewer checks each one.
  Added after OR-016a, where Gemini gave an unsourced refund time.
* ``too_long``: more than five sentences or list lines. The prompt asks for two to four.
* ``no_next_step``: neither of the reply's last two sentences is a question or an offer.
  Two, not one: "Could you share your order number? Once I have it, I can help." ends on
  a next step in substance.

The flags are a floor, not a verdict on tone. The transcripts are for a person to read
against each conversation's ``expect``. Exits 1 if any reply is flagged, so a run before
and after a prompt version change can be compared.

Not part of ``make check``: it needs a running service, a gateway and real model calls,
which cost money. Run it with ``make prompt-review`` (``BASE_URL`` / ``KEY`` to override).

**Conversations that need lookups.** A conversation marked ``"requires": "lookups"`` is
meaningful only against a deployment whose turns can look things up: air-tools-gateway
enabled, a planner that plans lookups (spec 0013) and a key whose turns carry a verified
customer (spec 0014). Elsewhere it would review a prompt rule that never fires, so it is
skipped and listed unless ``--lookups`` is given.

**Conversations that change something.** ``"requires": "actions"`` also needs a key with
``allow_actions``. A turn may be ``{"confirm": "last"}``: it confirms the last proposal
with the structured ``confirm`` field (spec 0001), sending the same id again when a
change has already been confirmed, which is how a replayed confirmation is reviewed. A
reply that is fixed text (a proposal, an execution outcome) calls no model and is not
flagged. Once the conversation shows a change was done, saying so is true, so
``claims_action`` is no longer flagged in it. Both kinds run only with ``--lookups``.

**Who the customer is.** Lookups need a verified customer (spec 0014): pass
``--air-token`` (or ``AIR_TOKEN``) with a token for the key's tenant.

Usage: ``prompt_review.py [--base-url URL] [--key KEY] [--air-token T] [--only NAME] [--lookups]``
"""

from __future__ import annotations

import argparse
import json
import os
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
#: ``_CLAIMS_ACTION`` less the verbs a turn with lookups may truthfully use.
_CLAIMS_CHANGE = re.compile(
    r"\bI(?:'ve| have| just)?\s+(?:gone ahead and\s+)?"
    r"(?:cancell?ed|refunded|processed|updated|changed|arranged|escalated|raised|submitted|"
    r"booked|rebooked|issued|initiated|requested|logged|flagged|reported)\b"
    r"|\b(?:has|have) been (?:cancell?ed|refunded|processed|escalated|arranged|issued|"
    r"submitted|rebooked|updated)\b",
    re.IGNORECASE,
)
_PROMISES_CHECK = re.compile(
    r"\bso (?:that )?(?:I|we) can\s+(?:look (?:into|up|at)|check|see (?:where|what)|find out|"
    r"pull up|track|trace|note|investigate|review)\b"
    r"|\b(?:I(?:'ll| will)|let me)\s+(?:look (?:into|up)|check|find out|pull up|track|"
    r"investigate)\b",
    re.IGNORECASE,
)
#: What a conversation's ``requires`` may name.
REQUIREMENTS = frozenset({"lookups", "actions"})
#: What a ``confirm`` turn sends as its message; the structured field is what counts.
_CONFIRM_TEXT = "Yes, please go ahead."
_OFFERS_TO_ACT = re.compile(
    r"\b(?:would you like|do you want|want|shall|should|can|may) (?:me|I) to\s+"
    r"(?:try (?:to |and )?)?(?:connect|transfer|flag|escalate|pass|point|put|get|send|forward|raise|log|arrange|"
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
#: A number, or a range, with a time unit: "3 business days", "5 to 10 working days".
_FIGURE = re.compile(
    r"\b\d+(?:\s*(?:to|-|–|or)\s*\d+)?\s*(?:working |business |calendar )?"
    r"(?:days?|weeks?|hours?|months?)\b",
    re.IGNORECASE,
)
_SEGMENTS = re.compile(r"(?<=[.!?])\s+|\n+")


def sentences(reply: str) -> list[str]:
    """Sentences and list lines, the units ``too_long`` counts."""
    return [part.strip() for part in _SEGMENTS.split(reply.strip()) if part.strip()]


def flags(reply: str, *, lookups: bool = False, changed: bool = False) -> list[str]:
    """The rules this reply breaks, by name; empty when it breaks none. ``lookups``:
    the conversation's turns can look things up, so only a claimed change counts.
    ``changed``: the conversation shows a change was done, so saying so is true."""
    found = []
    if not changed and (_CLAIMS_CHANGE if lookups else _CLAIMS_ACTION).search(reply):
        found.append("claims_action")
    if _OFFERS_TO_ACT.search(reply):
        found.append("offers_to_act")
    if not lookups and _PROMISES_CHECK.search(reply):
        found.append("promises_check")
    if _FIGURE.search(reply):
        found.append("states_figure")
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
        for turn in conversation["turns"]:
            if not isinstance(turn, str) and turn != {"confirm": "last"}:
                raise ValueError(f"{conversation['name']}: a turn is text or {{'confirm': 'last'}}")
        if conversation.get("requires", "lookups") not in REQUIREMENTS:
            raise ValueError(
                f"{conversation['name']}: unknown requires {conversation['requires']!r}"
            )
    return conversations


def _executed(turn: dict[str, Any]) -> bool:
    """The turn executed a confirmed change: its answer is the fixed outcome text."""
    return any(
        s.get("stage") == "gather" and (s.get("detail") or "").endswith(" done")
        for s in turn.get("trace", [])
    )


def run(
    base_url: str,
    key: str,
    only: str | None,
    *,
    lookups: bool = False,
    air_token: str | None = None,
) -> int:
    headers = {"X-API-Key": key, **({"air-token": air_token} if air_token else {})}
    flagged = 0
    skipped: list[str] = []
    with httpx.Client(base_url=base_url, timeout=90.0) as http:
        for conversation in load_review_set():
            if only and conversation["name"] != only:
                continue
            needs_lookups = conversation.get("requires") in REQUIREMENTS
            if needs_lookups and not lookups:
                skipped.append(conversation["name"])
                continue
            print(f"\n=== {conversation['name']}")
            print(f"    expect: {conversation.get('expect', '-')}")
            session_id: str | None = None
            proposal_id: str | None = None
            changed = False
            for step in conversation["turns"]:
                message = step if isinstance(step, str) else _CONFIRM_TEXT
                body: dict[str, Any] = {"message": message}
                if session_id:
                    body["session_id"] = session_id
                if not isinstance(step, str):
                    if proposal_id is None:
                        print(f"  customer: {message}\n  !! no proposal to confirm")
                        flagged += 1
                        break
                    body["confirm"] = {"proposal_id": proposal_id, "approve": True}
                response = http.post("/v1/chat", headers=headers, json=body)
                if response.status_code != httpx.codes.OK:
                    print(f"  customer: {message}\n  !! HTTP {response.status_code}")
                    flagged += 1
                    break
                turn = response.json()
                session_id = turn["session_id"]
                if turn.get("proposal"):
                    proposal_id = turn["proposal"]["proposal_id"]
                fixed = bool(turn.get("proposal")) or _executed(turn)
                changed = changed or _executed(turn)
                found = (
                    [] if fixed else flags(turn["answer"], lookups=needs_lookups, changed=changed)
                )
                flagged += bool(found)
                marker = f"  [{', '.join(found)}]" if found else ""
                print(f"  customer: {message}")
                print(f"  agent ({turn['status']}{', fixed text' if fixed else ''}){marker}:")
                for line in turn["answer"].splitlines() or [""]:
                    print(f"    {line}")
    if skipped:
        print(f"\nskipped (need lookups or actions; pass --lookups): {', '.join(skipped)}")
    print(f"\n{flagged} flagged repl{'y' if flagged == 1 else 'ies'}")
    return 1 if flagged else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--key", default="airo_local_customer_key")
    parser.add_argument("--only", help="run one conversation by name")
    parser.add_argument(
        "--air-token",
        default=os.environ.get("AIR_TOKEN"),
        help="customer token (spec 0014) sent as air-token; default $AIR_TOKEN",
    )
    parser.add_argument(
        "--lookups", action="store_true", help="also run conversations that need lookups"
    )
    args = parser.parse_args()
    return run(args.base_url, args.key, args.only, lookups=args.lookups, air_token=args.air_token)


if __name__ == "__main__":
    sys.exit(main())
