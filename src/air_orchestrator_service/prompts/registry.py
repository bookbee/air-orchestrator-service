"""A minimal prompt registry: versioned, pinned, built in.

A prompt is production logic (docs/00-plan.md's own framing) — pinned per
route rather than edited in place, so a prompt change is reviewable and
revertible like code. `PromptSettings.pins` (config.py) selects a version per
route; the default (`"latest"` in this module's own table) applies when a
route has no pin.

**Only a built-in default set, no `PromptSettings.registry_path` loading.**
A built-in default is what makes a fresh checkout answer without any prompt
files configured — the same "works before anyone edits anything" convention
every other AIR service's dev defaults follow. Loading versioned prompts from
external files is a real, separate piece of work (a file format, a reload
story) and is a documented follow-up, not built here.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

from air_orchestrator_service.config import Settings

__all__ = ["Prompt", "PromptNotFoundError", "PromptRegistry"]


class Prompt(BaseModel):
    route: str
    version: str
    system: str


class PromptNotFoundError(KeyError):
    pass


#: route -> version -> Prompt. `"latest"` is a real version, not an alias —
#: `_LATEST` below just says which one a caller with no pin gets.
_BUILTIN: Final[dict[str, dict[str, Prompt]]] = {
    "direct": {
        "v1": Prompt(
            route="direct",
            version="v1",
            system=(
                "You are the AIR platform's conversational front door. Answer the "
                "customer's message directly and concisely, in the language they "
                "used. You have no tools and no retrieved context in this turn — "
                "if the question needs information you don't have, say so plainly "
                "rather than guessing."
            ),
        ),
    },
}

_LATEST: Final[dict[str, str]] = {"direct": "v1"}


class PromptRegistry:
    """Looks up a route's pinned (or latest) prompt. Stateless and read-only —
    the same shape `ApiKeyStore` and `PromptRegistry`'s config-driven siblings
    already use elsewhere in this repo."""

    def __init__(self, settings: Settings) -> None:
        self._pins = settings.prompts.pins

    def get(self, route: str) -> Prompt:
        versions = _BUILTIN.get(route)
        if versions is None:
            raise PromptNotFoundError(f"no prompt registered for route '{route}'")
        version = self._pins.get(route, _LATEST[route])
        prompt = versions.get(version)
        if prompt is None:
            raise PromptNotFoundError(f"route '{route}' has no version '{version}'")
        return prompt
