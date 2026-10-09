"""The GitHub metered eval workflow supports weekly and manual runs."""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GH_EVAL = _REPO_ROOT / ".github" / "workflows" / "eval.yml"


def _gh_eval_workflow() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_GH_EVAL.read_text(encoding="utf-8")))


def _gh_on() -> dict[str, Any]:
    # PyYAML parses the unquoted ``on:`` key as the boolean True.
    workflow = _gh_eval_workflow()
    return cast("dict[str, Any]", workflow.get("on", workflow.get(True)))


def _gh_eval_job() -> dict[str, Any]:
    return cast("dict[str, Any]", _gh_eval_workflow()["jobs"]["eval"])


class TestGitHubEvalTriggers:
    def test_workflow_dispatch_is_a_trigger(self) -> None:
        assert "workflow_dispatch" in _gh_on(), (
            "The eval workflow must accept a manual `workflow_dispatch` trigger so the suite "
            "can run on demand from the Actions UI."
        )

    def test_schedule_is_a_trigger(self) -> None:
        on = _gh_on()
        assert "schedule" in on, "The eval workflow must run on a weekly schedule."
        crons = [entry["cron"] for entry in cast("list[dict[str, Any]]", on["schedule"])]
        assert crons, "The schedule trigger must declare at least one cron."

    def test_schedule_is_weekly_not_daily(self) -> None:
        on = _gh_on()
        crons = [entry["cron"] for entry in cast("list[dict[str, Any]]", on["schedule"])]
        # A weekly cron pins a day-of-week field (the 5th field) to a specific
        # weekday — `* * * * *`-style daily crons leave it as `*`.
        assert any(cron.split()[4] != "*" for cron in crons), (
            f"The metered eval must run weekly (a pinned day-of-week), not daily; got {crons}."
        )

    def test_backend_input_defaults_to_api_and_command_threads_it(self) -> None:
        # #3222 exposes a `backend` dispatch input so a CLI-free lane can be selected;
        # its DEFAULT is 'api', so the scheduled weekly run (empty input) is unchanged.
        inputs = cast("dict[str, Any]", _gh_on()["workflow_dispatch"]["inputs"])
        assert inputs["backend"]["default"] == "api"

        commands = "\n".join(
            step.get("with", {}).get("command", "") for step in cast("list[dict[str, Any]]", _gh_eval_job()["steps"])
        )
        # Trials is now a right-sizing input (default 2, was a hard-coded 3) threaded
        # via the EVAL_TRIALS env var so the subscription lane stays inside the window.
        assert '--trials "$EVAL_TRIALS"' in commands
        assert '--backend "$EVAL_BACKEND"' in commands

    def test_eval_job_runs_the_suite(self) -> None:
        for step in cast("list[dict[str, Any]]", _gh_eval_job()["steps"]):
            if "t3 eval run" in step.get("with", {}).get("command", ""):
                return
        msg = "the eval job must run the behavioral suite (`t3 eval run`)."
        raise AssertionError(msg)
