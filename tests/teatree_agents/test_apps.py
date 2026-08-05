"""``AgentsConfig.ready()`` registers runners without importing the agent SDKs.

``ready()`` runs inside every ``django.setup()``. Importing the runners there pulled
``teatree.agents.headless`` -> ``teatree.agents.harness`` ->
``pydantic_ai.models.openai`` -> the whole ``openai.types.*`` pydantic model tree, and
that import alone was ~10s of ``django.setup()`` on a loaded box — enough for
``t3 mcp serve`` to miss the MCP client's handshake window and be reported as a bare
``Connection closed``. These pin the deferral AND that the deferred runners still
resolve, so the fix cannot degrade into a registration that never dispatches.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.apps import apps

from teatree.agents.apps import run_headless_deferred, run_short_describe_deferred

_HEAVY_MODULES = ["openai", "pydantic_ai.models.openai", "teatree.agents.harness", "teatree.agents.headless"]

_INHERITED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "XDG_DATA_HOME", "T3_DATA_DIR")

_PROBE = f"""
import json, sys
from teatree.utils.django_bootstrap import ensure_django
ensure_django()
print(json.dumps([name for name in {_HEAVY_MODULES!r} if name in sys.modules]))
"""


def _probe_django_setup() -> list[str]:
    """Heavy modules resident after ``django.setup()`` in a FRESH interpreter.

    Asserting against the test process's own ``sys.modules`` would be vacuous — the
    suite imports the harness elsewhere, so the module is already resident and the
    check would pass whatever ``ready()`` does.
    """
    source_root = Path(__file__).resolve().parents[2] / "src"
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=False,
        env={
            **{name: os.environ[name] for name in _INHERITED_ENV if name in os.environ},
            "PYTHONPATH": str(source_root),
        },
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    for line in reversed(completed.stdout.splitlines()):
        if line.strip().startswith("["):
            return json.loads(line)
    msg = f"probe printed no JSON line.\nstdout: {completed.stdout!r}\nstderr: {completed.stderr!r}"
    raise AssertionError(msg)


class TestReadyDoesNotImportTheAgentSdks:
    def test_django_setup_loads_none_of_the_heavyweight_agent_modules(self) -> None:
        loaded = _probe_django_setup()

        assert loaded == [], f"django.setup() imported {loaded} — ready() must register, not import"

    def test_the_registered_headless_runner_still_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A thunk that never reaches the real runner would pass the guard above and ship nothing."""
        seen: list[tuple[object, str]] = []

        def fake_run_headless(task: object, *, phase: str, overlay_skill_metadata: object) -> str:
            seen.append((task, phase))
            return "attempt"

        monkeypatch.setattr("teatree.agents.headless.run_headless", fake_run_headless)

        result = run_headless_deferred("task", phase="coding", overlay_skill_metadata={})

        assert seen == [("task", "coding")]
        assert result == "attempt"

    def test_the_registered_short_describe_runner_still_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "teatree.agents.ticket_short_description.run_short_describe",
            lambda task: f"OK    {task}",
        )

        assert run_short_describe_deferred("task-7") == "OK    task-7"

    def test_ready_registers_both_runners(self) -> None:
        from teatree.core.deterministic_phases import deterministic_phase_runner  # noqa: PLC0415 — post-setup read
        from teatree.core.headless_dispatch import get_headless_runner  # noqa: PLC0415 — post-setup read
        from teatree.core.modelkit.phases import SHORT_DESCRIBE_PHASE  # noqa: PLC0415 — post-setup read

        apps.get_app_config("agents").ready()

        assert get_headless_runner() is run_headless_deferred
        assert deterministic_phase_runner(SHORT_DESCRIBE_PHASE) is run_short_describe_deferred
