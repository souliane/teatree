"""A job downstream of a skip-overriding job must override the upstream skip itself (#4048).

GitHub skips a job whose dependency was skipped, and that skip is TRANSITIVE: a job
that ran only because its own ``if`` named a status function (``always()`` or
``!cancelled()``) still passes the upstream skip on to its dependents. Only a dependent
that names one too escapes it — an explicit ``if`` that does not is evaluated *in
addition to*, not instead of, the inherited status gate.

Measured consequence before the fix: ``refresh-durations`` (needs ``test-shard``,
which overrides the skip because ``preflight`` is skipped on ``schedule``) was skipped
on EVERY scheduled run — runs 30803964519, 30740055489 and 30691949127, each with all
twelve shards green and the durations artifacts uploaded. Nothing failed, no refresh
PR was ever opened, and ``dev/.test_durations`` decayed to covering 11% of the test
files while pytest-split went on splitting the shard matrix from it.

The override must not be ``always()`` on a job a pull request can reach (#5006).
GitHub re-evaluates a running job's ``if`` when the run is cancelled and keeps the
job when it still holds, so ``always()`` made a superseded PR wave's shards run to
completion — 306 runner-minutes burned after cancels. ``!cancelled()`` overrides
the skip just the same and turns false once the run is cancelled.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests._actions_workflow import CI_WEEKLY_CRON, github_context, job_results, load, render, step_runs

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
_BASH = shutil.which("bash") or "/bin/bash"
_PULL_REQUEST = github_context("pull_request", ref="refs/pull/42/merge")
_WEEKLY = github_context("schedule", schedule=CI_WEEKLY_CRON)

_SKIP_OVERRIDES = ("always()", "!cancelled()")

# Schedule/dispatch-only, so never in a pull request's cancellable concurrency group.
_SCHEDULED_CANCEL_IMMUNE = {"refresh-durations", "scheduled-maintenance-report"}

_CANCELLABLE_SKIP_OVERRIDERS = ("test-shard", "jscpd-scan", "test")


def _jobs(workflow: Path) -> dict:
    return yaml.safe_load(workflow.read_text(encoding="utf-8")).get("jobs") or {}


def _needs(job: dict) -> list[str]:
    declared = job.get("needs") or []
    return [declared] if isinstance(declared, str) else list(declared)


def _condition(job: dict) -> str:
    return str(job.get("if", "") or "")


def _overrides_upstream_skip(job: dict) -> bool:
    return any(override in _condition(job) for override in _SKIP_OVERRIDES)


class TestNoJobInheritsAnUpstreamSkip:
    def test_every_dependent_of_a_skip_overriding_job_overrides_too(self) -> None:
        offenders = []
        for workflow in sorted(_WORKFLOWS.glob("*.yml")):
            jobs = _jobs(workflow)
            overriding = {name for name, job in jobs.items() if _overrides_upstream_skip(job)}
            for name, job in jobs.items():
                if _overrides_upstream_skip(job):
                    continue
                inherited = sorted(set(_needs(job)) & overriding)
                if inherited:
                    offenders.append(f"{workflow.name}:{name} needs {inherited}")
        assert not offenders, (
            "These jobs depend on a job that overrides an upstream skip, so GitHub propagates "
            "that skip to them and they never run — add `!cancelled() &&` to their own `if` "
            "(the explicit conditions after it still gate the job):\n  " + "\n  ".join(offenders)
        )


class TestSupersededPullRequestWavesStop:
    def test_no_job_a_pull_request_reaches_is_cancel_immune(self) -> None:
        jobs = _jobs(_WORKFLOWS / "ci.yml")
        immune = {name for name, job in jobs.items() if "always()" in _condition(job)}
        assert immune == _SCHEDULED_CANCEL_IMMUNE, (
            f"Cancel-immune ci.yml jobs: {sorted(immune)}. A job-level `always()` survives the "
            "supersede-cancel of a PR wave, so the superseded run keeps every runner it holds. "
            "Override an upstream skip with `!cancelled()` instead."
        )

    def test_the_cancel_immune_jobs_never_run_on_a_pull_request(self) -> None:
        results = job_results(load(), _PULL_REQUEST, outputs={"preflight": {"run_heavy_python": "true"}})
        reached = {name for name in _SCHEDULED_CANCEL_IMMUNE if results[name] != "skipped"}
        assert not reached, f"{sorted(reached)} keep `always()` only because no pull request reaches them"

    def test_a_superseded_wave_uploads_no_shard_artifacts_while_a_failed_leg_still_does(self) -> None:
        upload = next(
            step
            for step in load()["jobs"]["test-shard"]["steps"]
            if "shard-artifacts-" in str((step.get("with") or {}).get("name", ""))
        )
        context = {"github": _PULL_REQUEST}
        assert step_runs(upload, context, earlier_step_failed=True, cancelled=False), (
            "a leg whose tests failed must still hand its coverage and shard stats to the combiner"
        )
        assert not step_runs(upload, context, earlier_step_failed=False, cancelled=True), (
            "a cancelled, superseded wave must not spend runner time uploading artifacts nobody reads"
        )

    def test_the_heavy_lanes_override_the_skipped_preflight_with_not_cancelled(self) -> None:
        jobs = _jobs(_WORKFLOWS / "ci.yml")
        for name in _CANCELLABLE_SKIP_OVERRIDERS:
            assert "preflight" in _needs(jobs[name]), f"{name} must read the preflight lane decision"
            assert _condition(jobs[name]).lstrip().startswith("!cancelled() &&"), (
                f"{name} must override the skipped preflight on push/schedule with `!cancelled()`, "
                "or a push to main silently skips it."
            )


def _chain(head: dict[str, Any], *, tail_if: str | None = None) -> dict[str, Any]:
    tail = {"needs": "middle"} | ({} if tail_if is None else {"if": tail_if})
    return {"jobs": {"head": head, "middle": {"needs": "head", "if": "always()"}, "tail": tail}}


class TestTheEvaluatorPropagatesAnUpstreamSkip:
    """`job_results` reads status over every ancestor, as the runner does — not over direct needs."""

    def test_a_plain_dependent_of_a_skip_overrider_inherits_the_skip(self) -> None:
        results = job_results(_chain({"if": "false"}), _PULL_REQUEST)
        assert results == {"head": "skipped", "middle": "success", "tail": "skipped"}

    def test_a_dependent_naming_not_cancelled_escapes_it(self) -> None:
        results = job_results(_chain({"if": "false"}, tail_if="!cancelled()"), _PULL_REQUEST)
        assert results["tail"] == "success"

    def test_a_failure_two_levels_up_reaches_a_dependent_gated_on_failure(self) -> None:
        results = job_results(_chain({}, tail_if="failure()"), _PULL_REQUEST, failing=frozenset({"head"}))
        assert results == {"head": "failure", "middle": "success", "tail": "success"}


class TestRefreshDurationsStaysReachable:
    """The specific job whose silent skip left the shard split blind."""

    @pytest.mark.parametrize("failing", [frozenset(), frozenset({"test-shard"})], ids=["green", "red-shards"])
    def test_it_runs_on_the_weekly_run_whatever_the_shards_did(self, failing: frozenset[str]) -> None:
        # #4603: gating on a green lane was a second way to never run — the durations that
        # unbalance the split are what red the leg that then vetoed the refresh.
        results = job_results(load(), _WEEKLY, failing=failing)
        assert results["refresh-durations"] != "skipped"

    def test_without_its_always_the_skipped_preflight_reaches_it(self) -> None:
        workflow = load()
        refresh = workflow["jobs"]["refresh-durations"]
        refresh["if"] = str(refresh["if"]).replace("always() && ", "", 1)
        assert "always()" not in refresh["if"]

        results = job_results(workflow, _WEEKLY)
        assert results["test-shard"] == "success"
        assert results["refresh-durations"] == "skipped"


class TestARequiredCheckIsNeverSkippedByAFailedImageBuild:
    """A skipped required check passes branch protection, so `lint` must red when it has no image."""

    def test_lint_runs_when_build_image_failed(self) -> None:
        results = job_results(load(), _PULL_REQUEST, failing=frozenset({"build-image"}))
        assert results["lint"] != "skipped", "a skipped `lint` would let a pull request merge without lint"

    @pytest.mark.parametrize(
        ("build_image", "passes"),
        [("success", True), ("failure", False), ("skipped", False), ("cancelled", False)],
    )
    def test_its_first_step_reds_unless_the_image_was_built(
        self, build_image: str, *, passes: bool, tmp_path: Path
    ) -> None:
        guard = load()["jobs"]["lint"]["steps"][0]
        context = {"needs": {"build-image": {"result": build_image}}}
        env = {name: str(render(value, context)) for name, value in (guard.get("env") or {}).items()}
        result = subprocess.run(
            [_BASH, "-c", str(guard["run"])],
            capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), **env},
            cwd=tmp_path,
            check=False,
        )
        assert (result.returncode == 0) is passes, result.stderr
