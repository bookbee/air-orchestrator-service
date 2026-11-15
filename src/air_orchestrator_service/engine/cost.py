"""What a model call could cost, before it is made — spec 0008, ``REQ-COST-004``.

A pure function of settings, so the turn engine and the planner (spec 0013) price
their calls identically. Cost is otherwise only known once a call returns, which is
too late for a ceiling: each paid call is priced here first and skipped if the
estimate could take the turn past its ceiling.

**Why an upper bound, not a guess.** The prompt is counted generously from its
characters (``chars_per_token`` is low on purpose), and the whole ``max_tokens`` is
assumed spent. An estimate that runs high skips a call that would have fitted; one
that runs low overspends a ceiling a customer was promised.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from air_orchestrator_service.config import Settings

__all__ = ["estimate_cost"]


def estimate_cost(
    settings: Settings, messages: Iterable[Mapping[str, str]], max_tokens: int
) -> float:
    """An upper bound, in USD, on one call with these messages and ``max_tokens``."""
    rates = settings.turn.estimate
    chars = sum(len(message["content"]) for message in messages)
    prompt_tokens = chars / rates.chars_per_token
    return (
        prompt_tokens * rates.input_usd_per_mtok + max_tokens * rates.output_usd_per_mtok
    ) / 1_000_000
