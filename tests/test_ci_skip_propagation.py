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

from pathlib import Path

import yaml

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

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
        jobs = _jobs(_WORKFLOWS / "ci.yml")
        for name in sorted(_SCHEDULED_CANCEL_IMMUNE):
            condition = _condition(jobs[name])
            assert "github.event_name == 'schedule'" in condition, f"{name} must stay schedule-gated"
            assert "pull_request" not in condition, f"{name} keeps `always()` only because no PR reaches it"

    def test_the_heavy_lanes_override_the_skipped_preflight_with_not_cancelled(self) -> None:
        jobs = _jobs(_WORKFLOWS / "ci.yml")
        for name in _CANCELLABLE_SKIP_OVERRIDERS:
            assert "preflight" in _needs(jobs[name]), f"{name} must read the preflight lane decision"
            assert _condition(jobs[name]).lstrip().startswith("!cancelled() &&"), (
                f"{name} must override the skipped preflight on push/schedule with `!cancelled()`, "
                "or a push to main silently skips it."
            )


class TestRefreshDurationsStaysReachable:
    """The specific job whose silent skip left the shard split blind."""

    def test_it_runs_on_a_scheduled_run_whatever_the_shards_did(self) -> None:
        job = _jobs(_WORKFLOWS / "ci.yml")["refresh-durations"]
        condition = _condition(job)
        assert "always()" in condition
        assert "github.event_name == 'schedule'" in condition
        # #4603: gating on a green lane was a second way to never run — the durations that
        # unbalance the split are what red the leg that then vetoed the refresh.
        assert "needs.test-shard.result" not in condition
