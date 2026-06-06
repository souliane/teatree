"""The behavioral-eval CI jobs can be triggered manually on demand.

The weekly eval gate fires only on the first PR of the ISO week or the Sunday
cron backstop, so a maintainer who wants to run the suite at an arbitrary time
has no CI path — only the local ``t3 eval run``. These tests pin a manual
trigger on both mirrors: GitHub ``workflow_dispatch`` (with an optional backend
input) that forces ``run_eval=true``, and a GitLab ``when: manual`` eval job.
"""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GH_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_GITLAB_CI = _REPO_ROOT / ".gitlab-ci.yml"


def _gh_workflow() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_GH_WORKFLOW.read_text(encoding="utf-8")))


def _gh_on() -> dict[str, Any]:
    # PyYAML parses the unquoted ``on:`` key as the boolean True.
    workflow = _gh_workflow()
    return cast("dict[str, Any]", workflow.get("on", workflow.get(True)))


def _gh_eval_weekly() -> dict[str, Any]:
    return cast("dict[str, Any]", _gh_workflow()["jobs"]["eval-weekly"])


def _gh_gate_step_run() -> str:
    for step in cast("list[dict[str, Any]]", _gh_eval_weekly()["steps"]):
        if step.get("id") == "gate":
            return cast("str", step["run"])
    msg = "eval-weekly has no step with id `gate`."
    raise AssertionError(msg)


def _gitlab_config() -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load(_GITLAB_CI.read_text(encoding="utf-8")))


class TestGitHubManualTrigger:
    def test_workflow_dispatch_is_a_trigger(self) -> None:
        assert "workflow_dispatch" in _gh_on(), (
            "The workflow must accept a manual `workflow_dispatch` trigger so the eval "
            "suite can run on demand from the Actions UI."
        )

    def test_eval_weekly_if_covers_workflow_dispatch(self) -> None:
        assert "workflow_dispatch" in _gh_eval_weekly()["if"], (
            "eval-weekly's `if:` must include workflow_dispatch or the manual run never reaches the job."
        )

    def test_gate_step_forces_run_eval_on_dispatch(self) -> None:
        run = _gh_gate_step_run()
        assert "workflow_dispatch" in run, "The gate step must branch on the workflow_dispatch event."
        assert "run_eval=true" in run, "The dispatch branch must set run_eval=true unconditionally."

    def test_backend_input_defaults_to_sdk(self) -> None:
        inputs = cast("dict[str, Any]", _gh_on()["workflow_dispatch"]["inputs"])
        assert inputs["backend"]["default"] == "sdk", (
            "The optional `backend` input should default to sdk (the metered CI path)."
        )

    def test_pr_and_schedule_paths_are_preserved(self) -> None:
        condition = _gh_eval_weekly()["if"]
        assert "pull_request" in condition, "The manual trigger must be additive — the PR path stays."
        assert "schedule" in condition, "The manual trigger must be additive — the cron path stays."


class TestGitLabManualTrigger:
    def test_eval_manual_job_exists_with_when_manual(self) -> None:
        config = _gitlab_config()
        assert "eval-manual" in config, "GitLab must expose an `eval-manual` eval job variant."
        rules = cast("list[dict[str, Any]]", config["eval-manual"]["rules"])
        assert any(rule.get("when") == "manual" for rule in rules), (
            "The eval-manual job must carry a `when: manual` rule for on-demand parity."
        )

    def test_eval_manual_runs_the_suite(self) -> None:
        # The suite script is the shared `.eval-suite` body extended by eval-manual.
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
        # The manual job previously omitted the retry block; sharing `.eval-suite`
        # gives it the same bounded retry on transient infra classes.
        retry = cast("dict[str, Any]", _gitlab_config()[".eval-suite"]["retry"])
        assert retry["max"] == 2
        assert "script_failure" in retry["when"]
