#!/usr/bin/env python3
"""Score the planner prompt against a live air-model-gateway — spec 0013, ``REQ-PLN-009``.

Each case in ``evals/planner/cases.json`` is a message (with optional history, classifier
labels and a narrower menu) and the plan it should produce. This builds the same
``ModelPlanner`` the service builds, from ``.env`` like ``make run``, and plans each case
with one real call, then a second when round 1 asks for results and the case gives some.
It prints one row per case and the mismatches, and exits 1 if any case mismatched, so a
run before and after a prompt version change can be compared.

**Why the planner, not the service.** A plan is never shown to a client
(``REQ-PLN-013``), so it cannot be scored through ``/v1/chat``. Calling the planner
directly also needs no air-tools-gateway: the menu comes from the case file, which keeps
the cases about planning and nothing else.

**What a match is.** Read names in any order; for each capability named under
``arguments``, each listed key equal; ``propose`` by name; ``needs_results`` as given. A
key the case leaves out is not scored. Matching is deliberately loose about extra
arguments: a plan that adds ``limit`` is still the right plan.

Not part of ``make check``: real model calls cost money. Run it with ``make
planner-eval``, ``ONLY=<case>`` for one case and ``MODEL_CLASS=local`` to try another class.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from dataclasses import dataclass
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "planner" / "cases.json"
sys.path.insert(0, str(ROOT / "src"))

from air_orchestrator_service.clients.model_gateway import ModelGatewayClient  # noqa: E402
from air_orchestrator_service.clients.tools_gateway import (  # noqa: E402
    Inventory,
    _check_entry,
)
from air_orchestrator_service.config import Settings  # noqa: E402
from air_orchestrator_service.constants import Channel, StageStatus  # noqa: E402
from air_orchestrator_service.engine.plan import (  # noqa: E402
    ModelPlanner,
    Plan,
    PlanContext,
    Planner,
    Step,
)
from air_orchestrator_service.guardrails.boundary import delimit  # noqa: E402
from air_orchestrator_service.prompts.registry import PromptRegistry  # noqa: E402
from air_orchestrator_service.schemas.common import Principal  # noqa: E402


@dataclass
class Row:
    name: str
    got: str
    mismatches: list[str]
    cost_usd: float


def load(path: pathlib.Path = CASES) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(path.read_text())
    return doc


def _inventory(entries: list[dict[str, Any]], names: list[str] | None) -> Inventory:
    checked = [_check_entry(entry)[0] for entry in entries]
    return Inventory(
        capabilities=tuple(
            c for c in checked if c is not None and (names is None or c.name in names)
        )
    )


def _context(case: dict[str, Any], menu: list[dict[str, Any]], deadline_s: float) -> PlanContext:
    principal = Principal(
        key_id="planner-eval",
        name="planner-eval",
        channel=Channel.CUSTOMER,
        tenant="eval",
        allow_actions=case.get("allow_actions", True),
        subject="eval_subject",
    )
    return PlanContext(
        text=case["message"],
        principal=principal,
        confirming=False,
        rag_serves=False,
        offered=_inventory(menu, case.get("menu")),
        history=tuple((role, content) for role, content in case.get("history", [])),
        intent=case.get("intent"),
        sentiment=case.get("sentiment"),
        # The deployment's own turn deadline, so a case is bounded as a turn would be.
        deadline_s=deadline_s,
    )


def _args_mismatch(expected: dict[str, dict[str, Any]], steps: list[Step]) -> list[str]:
    out = []
    for name, wanted in expected.items():
        step = next((s for s in steps if s.capability == name), None)
        if step is None:
            continue  # the name check reports a missing step
        for key, value in wanted.items():
            if step.arguments.get(key) != value:
                out.append(f"{name}.{key}={step.arguments.get(key)!r}, wanted {value!r}")
    return out


def score(case: dict[str, Any], plan: Plan, second: Step | str | None = "-") -> list[str]:
    """What differs from the case's expectation, or ``[]``. ``second`` is round 2's
    proposal, ``None`` for no change, or ``"-"`` when round 2 did not run."""
    expect = case["expect"]
    out: list[str] = []
    steps = list(plan.reads) + ([plan.write] if plan.write is not None else [])
    if "reads" in expect and not case.get("allow_any_reads"):
        got = sorted(step.capability for step in plan.reads)
        if got != sorted(expect["reads"]):
            out.append(f"reads={got}, wanted {sorted(expect['reads'])}")
    if "propose" in expect:
        got_write = plan.write.capability if plan.write is not None else None
        if got_write != expect["propose"]:
            out.append(f"propose={got_write}, wanted {expect['propose']}")
    if "needs_results" in expect and plan.needs_results is not expect["needs_results"]:
        out.append(f"needs_results={plan.needs_results}, wanted {expect['needs_results']}")
    out += _args_mismatch(expect.get("arguments", {}), steps)
    round_two = case.get("round_two")
    if round_two is not None:
        if second == "-":
            out.append("round 2 did not run")
        else:
            assert not isinstance(second, str)
            wanted = round_two["expect"].get("propose")
            got_write = second.capability if second is not None else None
            if got_write != wanted:
                out.append(f"round 2 propose={got_write}, wanted {wanted}")
            if second is not None:
                out += [
                    f"round 2 {m}"
                    for m in _args_mismatch(round_two["expect"].get("arguments", {}), [second])
                ]
    return out


def _describe(plan: Plan, second: Step | str | None) -> str:
    parts = [f"reads={','.join(s.capability for s in plan.reads) or '-'}"]
    parts.append(f"propose={plan.write.capability if plan.write else '-'}")
    if plan.needs_results:
        parts.append("needs_results")
    if second != "-":
        parts.append(f"round2={second.capability if isinstance(second, Step) else '-'}")
    return " ".join(parts)


async def run_case(
    planner: Planner, case: dict[str, Any], menu: list[dict[str, Any]], *, deadline_s: float = 15.0
) -> Row:
    ctx = _context(case, menu, deadline_s)
    outcome = await planner.plan(ctx)
    cost = outcome.usage.cost_usd if outcome.usage else 0.0
    if outcome.status is not StageStatus.OK:
        return Row(case["name"], f"no plan: {outcome.detail}", [f"no plan: {outcome.detail}"], cost)
    plan = outcome.plan
    second: Step | str | None = "-"
    round_two = case.get("round_two")
    if round_two is not None and plan.needs_results and plan.reads:
        results = delimit(round_two["results"], source="tool_result")
        follow = await planner.follow_up(ctx, plan, results)
        cost += follow.usage.cost_usd if follow.usage else 0.0
        if follow.failed:
            return Row(case["name"], follow.detail, [follow.detail], cost)
        second = follow.write
    return Row(case["name"], _describe(plan, second), score(case, plan, second), cost)


async def run(
    planner: Planner, doc: dict[str, Any], only: str | None = None, *, deadline_s: float = 15.0
) -> list[Row]:
    cases = [c for c in doc["cases"] if only is None or c["name"] == only]
    return [await run_case(planner, case, doc["menu"], deadline_s=deadline_s) for case in cases]


def report(rows: list[Row], out: Any = sys.stdout) -> int:
    width = max((len(r.name) for r in rows), default=4)
    for row in rows:
        mark = "ok  " if not row.mismatches else "MISS"
        print(f"{mark} {row.name:<{width}}  {row.got}", file=out)
        for mismatch in row.mismatches:
            print(f"     {'':<{width}}  - {mismatch}", file=out)
    missed = sum(1 for r in rows if r.mismatches)
    cost = sum(r.cost_usd for r in rows)
    print(f"\n{len(rows) - missed}/{len(rows)} matched, ${cost:.4f} spent", file=out)
    return 1 if missed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="run one case by name")
    parser.add_argument("--model-class", help="plan with this class instead of the setting")
    args = parser.parse_args()

    settings = Settings()
    planner_settings = settings.planner.model_copy(update={"enabled": True})
    if args.model_class:
        planner_settings = planner_settings.model_validate(
            {**planner_settings.model_dump(), "model_class": args.model_class}
        )
    settings = settings.model_copy(update={"planner": planner_settings})
    gateway = ModelGatewayClient(settings)

    async def go() -> list[Row]:
        try:
            planner = ModelPlanner(settings, gateway, PromptRegistry(settings))
            return await run(
                planner, load(), args.only, deadline_s=settings.turn.deadline_ms / 1000
            )
        finally:
            await gateway.aclose()

    print(
        f"planner:{PromptRegistry(settings).get('planner').version} "
        f"model_class={planner_settings.model_class.value}\n"
    )
    return report(asyncio.run(go()))


if __name__ == "__main__":
    sys.exit(main())
