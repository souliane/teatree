"""The metered behavioral-eval workflow runs weekly + on demand, off the PR path.

The metered ``claude -p`` suite lives in a standalone workflow
(``.github/workflows/eval.yml`` / a GitLab schedule + manual job) so a PR
pipeline neither runs nor displays a metered-eval check. These tests pin the
weekly schedule and the on-demand manual trigger on both mirrors: GitHub
``schedule`` + ``workflow_dispatch`` (with the SDK backend fixed in the
workflow command), and a GitLab schedule + ``when: manual`` eval job.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GH_EVAL = _REPO_ROOT / ".github" / "workflows" / "eval.yml"
_GITLAB_CI = _REPO_ROOT / ".gitlab-ci.yml"


def _gh_eval_workflow() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_GH_EVAL.read_text(encoding="utf-8")))


def _gh_on() -> dict[str, Any]:
    # PyYAML parses the unquoted ``on:`` key as the boolean True.
    workflow = _gh_eval_workflow()
    return cast("dict[str, Any]", workflow.get("on", workflow.get(True)))


def _gh_eval_job() -> dict[str, Any]:
    return cast("dict[str, Any]", _gh_eval_workflow()["jobs"]["eval"])


def _gitlab_config() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_GITLAB_CI.read_text(encoding="utf-8")))


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

    def test_backend_is_fixed_to_api_for_trials(self) -> None:
        inputs = cast("dict[str, Any]", _gh_on()["workflow_dispatch"]["inputs"])
        assert "backend" not in inputs, "`--trials` is always a fresh SDK run — the backend is never an input."

        commands = "\n".join(
            step.get("with", {}).get("command", "") for step in cast("list[dict[str, Any]]", _gh_eval_job()["steps"])
        )
        # Trials is now a right-sizing input (default 2, was a hard-coded 3) threaded
        # via the EVAL_TRIALS env var so the subscription lane stays inside the window.
        assert '--trials "$EVAL_TRIALS"' in commands
        assert "--backend api" in commands

    def test_eval_job_runs_the_suite(self) -> None:
        for step in cast("list[dict[str, Any]]", _gh_eval_job()["steps"]):
            if "t3 eval run" in step.get("with", {}).get("command", ""):
                return
        msg = "the eval job must run the behavioral suite (`t3 eval run`)."
        raise AssertionError(msg)


class TestGitLabEvalTriggers:
    def test_eval_manual_job_exists_with_when_manual(self) -> None:
        config = _gitlab_config()
        assert "eval-manual" in config, "GitLab must expose an `eval-manual` eval job variant."
        rules = cast("list[dict[str, Any]]", config["eval-manual"]["rules"])
        assert any(rule.get("when") == "manual" for rule in rules), (
            "The eval-manual job must carry a `when: manual` rule for on-demand parity."
        )

    def test_eval_weekly_runs_on_schedule(self) -> None:
        config = _gitlab_config()
        rules = cast("list[dict[str, Any]]", config["eval-weekly"]["rules"])
        assert any("schedule" in rule.get("if", "") for rule in rules), (
            "The eval-weekly job must fire on a GitLab pipeline schedule."
        )

    def test_eval_runs_the_suite(self) -> None:
        # The suite script is the shared `.eval-suite` body extended by the jobs.
        script = "\n".join(cast("list[str]", _gitlab_config()[".eval-suite"]["script"]))
        assert "t3 eval run" in script, "the shared eval-suite must run the behavioral suite."

    def test_eval_jobs_share_one_suite_definition(self) -> None:
        # DRY: both eval jobs extend the single `.eval-suite` template rather than
        # carrying their own copy of image/before_script/script/retry.
        config = _gitlab_config()
        assert ".eval-suite" in config, "the shared eval-suite template must exist."
        for job in ("eval-weekly", "eval-manual"):
            assert config[job].get("extends") == ".eval-suite", f"{job} must extend the shared eval-suite."
            assert "script" not in config[job], f"{job} must not carry its own script copy."
            assert "retry" not in config[job], f"{job} must not carry its own retry copy."

    def test_eval_manual_inherits_the_transient_retry(self) -> None:
        # The shared `.eval-suite` gives every eval job the same bounded retry on
        # transient infra classes.
        retry = cast("dict[str, Any]", _gitlab_config()[".eval-suite"]["retry"])
        assert retry["max"] == 2
        assert "script_failure" in retry["when"]
