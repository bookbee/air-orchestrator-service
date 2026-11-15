"""The planner eval runner — spec 0013 T7 (``REQ-PLN-009``, evaluation half).

The live run is ``make planner-eval``. This runs the same runner over the real case
file with a scripted model in place of air-model-gateway, so a clean live run means
something: right plans score as matches, wrong ones are named, round 2 is driven, and
the exit code says which.
"""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys
from types import ModuleType
from typing import Any

from air_orchestrator_service.clients.model_gateway import GenerateResult, GenerateUsage
from air_orchestrator_service.config import Settings
from air_orchestrator_service.engine.plan import ModelPlanner
from air_orchestrator_service.prompts.registry import PromptRegistry

_TOOL = pathlib.Path(__file__).resolve().parents[2] / "tools" / "planner_eval.py"


def _eval() -> ModuleType:
    spec = importlib.util.spec_from_file_location("planner_eval", _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["planner_eval"] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


class _Scripted:
    """A gateway whose planning answers come from a function of the call."""

    def __init__(self, answer: Any) -> None:
        self._answer = answer
        self.calls = 0

    async def generate(self, **kwargs: Any) -> GenerateResult:
        self.calls += 1
        return GenerateResult(
            content=json.dumps(self._answer(kwargs)),
            cost_usd=0.001,
            finish_reason="stop",
            usage=GenerateUsage(prompt_tokens=1, completion_tokens=1),
        )


def _planner(answer: Any) -> tuple[ModelPlanner, _Scripted]:
    settings = Settings.model_validate({"app": {"env": "test"}, "planner": {"enabled": True}})
    gateway = _Scripted(answer)
    return ModelPlanner(settings, gateway, PromptRegistry(settings)), gateway  # type: ignore[arg-type]


def _expected(case: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """The plan the case asks for, as the model would write it."""
    expect = case["expect"]
    args = expect.get("arguments", {})
    if "reads" not in schema["properties"]:  # round 2
        two = case["round_two"]["expect"]
        name = two.get("propose")
        step = {"capability": name, "arguments": two.get("arguments", {}).get(name, {})}
        return {"propose": step if name else None, "reason": "r"}
    reads = [] if case.get("allow_any_reads") else expect.get("reads", [])
    propose = expect.get("propose")
    return {
        "reads": [{"capability": n, "arguments": args.get(n, {})} for n in reads],
        "propose": {"capability": propose, "arguments": args.get(propose, {})} if propose else None,
        "needs_results": expect.get("needs_results", False),
        "reason": "r",
    }


async def test_the_case_file_is_well_formed_and_a_perfect_planner_matches_every_case() -> None:
    tool = _eval()
    doc = tool.load()
    assert len(doc["cases"]) >= 20
    assert len({case["name"] for case in doc["cases"]}) == len(doc["cases"])
    current: dict[str, Any] = {}

    def answer(kwargs: dict[str, Any]) -> dict[str, Any]:
        assert current["case"]["message"] in kwargs["messages"][-1]["content"]
        return _expected(current["case"], kwargs["json_schema"])

    planner, gateway = _planner(answer)
    rows = []
    for case in doc["cases"]:
        current["case"] = case
        rows.append(await tool.run_case(planner, case, doc["menu"]))

    out = io.StringIO()
    assert tool.report(rows, out) == 0, out.getvalue()
    total = len(doc["cases"])
    assert f"{total}/{total} matched" in out.getvalue()
    two_rounds = sum(1 for c in doc["cases"] if c.get("round_two"))
    assert gateway.calls == total + two_rounds


async def test_a_wrong_plan_is_named_and_fails_the_run() -> None:
    tool = _eval()
    doc = tool.load()
    doc = {
        **doc,
        "cases": [
            c for c in doc["cases"] if c["name"] in ("status-with-number", "cancel-last-order")
        ],
    }

    def wrong(kwargs: dict[str, Any]) -> dict[str, Any]:
        if "reads" not in kwargs["json_schema"]["properties"]:
            return {"propose": None, "reason": "r"}
        return {
            "reads": [{"capability": "orders.get", "arguments": {"order_id": "99999"}}],
            "propose": None,
            "needs_results": True,
            "reason": "r",
        }

    planner, _ = _planner(wrong)
    rows = await tool.run(planner, doc)

    out = io.StringIO()
    assert tool.report(rows, out) == 1
    text = out.getvalue()
    assert "orders.get.order_id='99999', wanted '48213'" in text
    assert "needs_results=True, wanted False" in text
    assert "reads=['orders.get'], wanted ['orders.list_recent']" in text
    assert "round 2 propose=None, wanted orders.cancel" in text
    assert "0/2 matched" in text
