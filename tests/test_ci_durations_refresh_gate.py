"""The durations refresh must be reachable on demand, not only on the daily cron (#4048).

``refresh-durations`` has never produced a single PR. The ``always()`` fix removed the
reason, but left the path reachable from one place only: a cron that fires once a day on
``main``. That makes its first-ever execution a one-shot experiment with 24h between
attempts — and everything downstream waits on it. ``dev/.test_durations`` covers 11% of
the test files, pytest-split bin-packs the other 89% at the average, and the shard that
draws the slow ones reds whichever PR is in flight. Sizing the per-test ceilings needs the
refreshed data too, so the whole epic queues behind a job nobody can run.

So the same path is reachable by an explicit ask. Three sites gate it — the shard's
``--store-durations``, the shard's durations upload, and the refresh job itself — and they
must agree: a shard that records nothing leaves the merge job an empty union, and a merge
job that does not run leaves twelve uploaded artifacts unread. The gate is pinned verbatim
at all three rather than three conditions that happen to coincide today.

``github.ref`` is part of it because the refresh job force-pushes ``ci/test-durations-refresh``
and opens a PR against ``main``. A cron always runs on the default branch; a dispatch can
name any ref, and one dispatched from a feature branch would force-push that branch's
commits over the real refresh branch.
"""

from pathlib import Path

import yaml

_CI = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"

# One expression, repeated verbatim at every site on the refresh path.
DURATIONS_GATE = "(github.ref == 'refs/heads/main' && (github.event_name == 'schedule' || inputs.refresh_durations))"

_DISPATCH_INPUT = "refresh_durations"


def _workflow() -> dict:
    return yaml.safe_load(_CI.read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:
    # `on` is a YAML 1.1 boolean, so safe_load keys the trigger block under True.
    return workflow.get("on") or workflow[True]


def _steps(job: dict) -> list[dict]:
    return list(job.get("steps") or [])


def _step_containing(job: dict, needle: str, key: str) -> dict:
    matches = [step for step in _steps(job) if needle in str(step.get(key, ""))]
    assert len(matches) == 1, f"expected exactly one step whose `{key}` mentions {needle!r}, found {len(matches)}"
    return matches[0]


class TestTheRefreshIsReachableOnDemand:
    def test_the_workflow_declares_a_refresh_durations_dispatch_input(self) -> None:
        dispatch = _triggers(_workflow()).get("workflow_dispatch")
        assert dispatch is not None, (
            "ci.yml has no `workflow_dispatch` trigger, so `refresh-durations` can only ever run "
            "on the daily cron — one attempt a day at a job that has never successfully produced a PR."
        )
        assert _DISPATCH_INPUT in (dispatch.get("inputs") or {}), (
            f"`workflow_dispatch` declares no `{_DISPATCH_INPUT}` input, so a manual run cannot ask "
            "for the durations refresh."
        )

    def test_asking_is_explicit_so_an_unrelated_dispatch_records_nothing(self) -> None:
        spec = _triggers(_workflow())["workflow_dispatch"]["inputs"][_DISPATCH_INPUT]
        assert spec.get("type") == "boolean", (
            "the input must be typed `boolean`: a string input reaches expressions as 'true'/'false', "
            "and the string 'false' is truthy — every dispatch would open a refresh PR."
        )
        assert spec.get("default") is False, (
            "a dispatch run for any other reason must not force-push the refresh branch — "
            "recording durations is opt-in."
        )


class TestEveryGateOnTheRefreshPathAgrees:
    """Three gates, one expression. A disagreement is a half-run refresh that reads as a no-op."""

    def test_the_shard_records_fresh_durations_behind_the_gate(self) -> None:
        job = _workflow()["jobs"]["test-shard"]
        run = str(_step_containing(job, "--store-durations", "run")["run"])
        assert f"{DURATIONS_GATE} && '--store-durations --clean-durations'" in run, (
            "the shard's `--store-durations` is not guarded by the shared gate — a recording run "
            f"whose gate has drifted stores nothing for the merge job to union.\n  gate: {DURATIONS_GATE}"
        )

    def test_the_shard_uploads_what_it_recorded_behind_the_same_gate(self) -> None:
        job = _workflow()["jobs"]["test-shard"]
        step = _step_containing(job, "durations-shard-", "with")
        assert str(step.get("if", "")).strip() == f"always() && {DURATIONS_GATE}", (
            "the durations upload runs on a different condition from the recording that feeds it — "
            "the pair must open and close together. `always()` is what makes the upload survive a "
            "FAILED shard: a step whose `if` names no status function inherits `success()`, so the "
            "legs worth hearing from most uploaded nothing (#4603)."
        )

    def test_the_refresh_job_runs_on_the_same_gate(self) -> None:
        condition = str(_workflow()["jobs"]["refresh-durations"].get("if", "")).strip()
        assert condition == f"always() && {DURATIONS_GATE}", (
            "`refresh-durations` no longer carries the shared gate verbatim. `always()` stays first "
            "(it is what stops GitHub propagating `preflight`'s skip through `test-shard`)."
        )

    def test_the_refresh_job_does_not_wait_for_a_green_lane(self) -> None:
        # #4603: `needs.test-shard.result == 'success'` made the durations self-blocking. Stale
        # durations bin-pack the unrecorded tests at the average, whichever leg draws the slow
        # ones exceeds the ceiling, and that leg then vetoed the job that un-stales them.
        condition = str(_workflow()["jobs"]["refresh-durations"].get("if", "")).strip()
        assert "needs.test-shard.result" not in condition, (
            "`refresh-durations` requires the shard lane to have passed again. Recording is a "
            "measurement, not a verdict — a leg that timed out still timed everything it ran, and "
            "a red lane is exactly when the durations most need refreshing."
        )
