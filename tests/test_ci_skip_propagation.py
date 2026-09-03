"""A job downstream of an ``always()`` job must re-establish ``always()`` itself (#4048).

GitHub skips a job whose dependency was skipped, and that skip is TRANSITIVE: a job
that ran only because its own ``if`` said ``always()`` still passes the upstream skip
on to its dependents. Only a dependent that says ``always()`` too escapes it — an
explicit ``if`` that does not mention it is evaluated *in addition to*, not instead
of, the inherited status gate.

Measured consequence before the fix: ``refresh-durations`` (needs ``test-shard``,
which uses ``always()`` because ``preflight`` is skipped on ``schedule``) was skipped
on EVERY scheduled run — runs 30803964519, 30740055489 and 30691949127, each with all
twelve shards green and the durations artifacts uploaded. Nothing failed, no refresh
PR was ever opened, and ``dev/.test_durations`` decayed to covering 11% of the test
files while pytest-split went on splitting the shard matrix from it.
"""

from pathlib import Path

import yaml

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _jobs(workflow: Path) -> dict:
    return yaml.safe_load(workflow.read_text(encoding="utf-8")).get("jobs") or {}


def _needs(job: dict) -> list[str]:
    declared = job.get("needs") or []
    return [declared] if isinstance(declared, str) else list(declared)


def _condition(job: dict) -> str:
    return str(job.get("if", "") or "")


class TestNoJobInheritsAnUpstreamSkip:
    def test_every_dependent_of_an_always_job_says_always_too(self) -> None:
        offenders = []
        for workflow in sorted(_WORKFLOWS.glob("*.yml")):
            jobs = _jobs(workflow)
            always = {name for name, job in jobs.items() if "always()" in _condition(job)}
            for name, job in jobs.items():
                if "always()" in _condition(job):
                    continue
                inherited = sorted(set(_needs(job)) & always)
                if inherited:
                    offenders.append(f"{workflow.name}:{name} needs {inherited}")
        assert not offenders, (
            "These jobs depend on a job that runs via `always()`, so GitHub propagates the "
            "upstream skip to them and they never run — add `always() &&` to their own `if` "
            "(the explicit conditions after it still gate the job):\n  " + "\n  ".join(offenders)
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
