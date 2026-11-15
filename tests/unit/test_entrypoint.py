"""The process the console script starts — spec 0015.

Sessions live in one dict per process (``memory/session.py``), so the worker count is a
correctness setting, not a tuning one: with two, a follow-up that reaches the other worker
is a ``404 No such session`` (air-web-console SIT, OR-012).
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from structlog.testing import capture_logs

from air_orchestrator_service import __main__ as entrypoint
from air_orchestrator_service.config import AppSettings, Settings

_EXAMPLE = pathlib.Path(__file__).resolve().parents[2] / ".env.example"


@pytest.mark.parametrize("backend", ["memory", "redis"])
def test_an_in_process_session_store_runs_one_worker(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """``REQ-SPS-001``, AC-1. Two configured workers start as one, with one warning
    naming both counts; ``redis`` is not built yet and falls back to memory, so it is
    in-process too. One configured worker starts as one, with no warning."""
    started: list[dict[str, Any]] = []
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kw: started.append(kw))

    def boot(workers: int) -> list[dict[str, Any]]:
        settings = Settings.model_validate(
            {"app": {"env": "test", "workers": workers}, "session": {"backend": backend}}
        )
        monkeypatch.setattr(entrypoint, "get_settings", lambda: settings)
        with capture_logs() as logs:
            entrypoint.main()
        return [entry for entry in logs if entry["event"] == "app.workers_reduced"]

    [warning] = boot(2)
    assert started[-1]["workers"] == 1
    assert (warning["log_level"], warning["configured"], warning["used"]) == ("warning", 2, 1)
    assert boot(1) == []
    assert started[-1]["workers"] == 1


def test_one_worker_is_the_default_and_the_example() -> None:
    """``REQ-SPS-002``: a fresh checkout needs no warning to be correct."""
    assert AppSettings().workers == 1
    assert "AIR_ORCHESTRATOR_SERVICE__APP__WORKERS=1\n" in _EXAMPLE.read_text()
