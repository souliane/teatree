"""Unit tests for the provision-smoke harness (#1308).

The harness orchestrates a fixed sequence of ``t3 <overlay> ...`` steps and
produces a categorised :class:`SmokeReport`. Tests inject a fake step
runner so the suite never shells out — the live run would take minutes
and depend on Docker / overlay infra.
"""

from collections.abc import Iterable

import pytest

from teatree.loop.dogfood_smoke import (
    STEP_OUTCOME_KIND,
    SmokeOutcomeKind,
    SmokeReport,
    SmokeStep,
    StepResult,
    default_steps,
    report_summary,
    run_smoke,
)


def _step_result(step: SmokeStep, *, returncode: int = 0, stderr: str = "", timed_out: bool = False) -> StepResult:
    return StepResult(
        step=step,
        returncode=returncode,
        stderr=stderr,
        stdout="",
        elapsed_seconds=0.01,
        timed_out=timed_out,
    )


class _ScriptedRunner:
    """Replay a scripted list of ``(returncode, stderr, timed_out)`` per step name."""

    def __init__(self, plan: dict[str, tuple[int, str, bool]]) -> None:
        self.plan = plan
        self.calls: list[str] = []

    def __call__(self, step: SmokeStep) -> StepResult:
        self.calls.append(step.name)
        rc, stderr, timed_out = self.plan.get(step.name, (0, "", False))
        return _step_result(step, returncode=rc, stderr=stderr, timed_out=timed_out)


def _all_green(steps: Iterable[SmokeStep]) -> dict[str, tuple[int, str, bool]]:
    return {step.name: (0, "", False) for step in steps}


class TestDefaultSteps:
    def test_sequence_order_provision_then_start_then_ready_then_teardown_then_clean(self) -> None:
        names = [step.name for step in default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")]
        assert names == [
            "workspace_ticket",
            "env_show",
            "worktree_provision",
            "worktree_start",
            "worktree_ready",
            "worktree_teardown",
            "workspace_clean_all",
        ]

    def test_workspace_ticket_carries_fixture_url_and_variant(self) -> None:
        step = default_steps(
            overlay="testoverlay",
            fixture_ticket_url="https://github.com/souliane/teatree/issues/1308",
            variant="test-variant",
        )[0]
        assert step.name == "workspace_ticket"
        assert "https://github.com/souliane/teatree/issues/1308" in step.command
        assert "--variant" in step.command
        assert "test-variant" in step.command

    def test_workspace_ticket_omits_variant_when_unset(self) -> None:
        step = default_steps(
            overlay="testoverlay",
            fixture_ticket_url="https://x/issues/1",
        )[0]
        assert "--variant" not in step.command

    def test_step_commands_use_provided_overlay_name(self) -> None:
        steps = default_steps(overlay="customoverlay", fixture_ticket_url="https://x/issues/1")
        for step in steps:
            assert step.command[1] == "customoverlay"


class TestRunSmoke:
    def test_all_steps_pass_marks_report_pass(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        runner = _ScriptedRunner(_all_green(steps))

        report = run_smoke(steps, runner=runner)

        assert report.passed is True
        assert report.outcome is SmokeOutcomeKind.PASS
        assert report.failing_step == ""
        assert len(report.steps) == len(steps)
        assert runner.calls == [step.name for step in steps]

    def test_orchestration_calls_each_step_in_order(self) -> None:
        steps = [
            SmokeStep(name="a", command=("a",)),
            SmokeStep(name="b", command=("b",)),
            SmokeStep(name="c", command=("c",)),
        ]
        runner = _ScriptedRunner(_all_green(steps))

        run_smoke(steps, runner=runner)

        assert runner.calls == ["a", "b", "c"]

    def test_provision_failure_categorised_as_provision_failed(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_provision"] = (1, "dslr alias missing\n", False)
        runner = _ScriptedRunner(plan)

        report = run_smoke(steps, runner=runner)

        assert report.outcome is SmokeOutcomeKind.PROVISION_FAILED
        assert report.failing_step == "worktree_provision"
        assert "dslr alias missing" in report.failing_step_stderr

    def test_start_failure_categorised_as_start_failed(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_start"] = (1, "docker boom", False)
        runner = _ScriptedRunner(plan)

        report = run_smoke(steps, runner=runner)

        assert report.outcome is SmokeOutcomeKind.START_FAILED
        assert report.failing_step == "worktree_start"

    def test_ready_failure_categorised_as_ready_failed(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_ready"] = (1, "health 503", False)
        runner = _ScriptedRunner(plan)

        report = run_smoke(steps, runner=runner)

        assert report.outcome is SmokeOutcomeKind.READY_FAILED
        assert report.failing_step == "worktree_ready"

    def test_teardown_failure_categorised_as_teardown_failed(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_teardown"] = (1, "container still up", False)
        runner = _ScriptedRunner(plan)

        report = run_smoke(steps, runner=runner)

        assert report.outcome is SmokeOutcomeKind.TEARDOWN_FAILED
        assert report.failing_step == "worktree_teardown"

    def test_timeout_categorised_as_timeout_and_stops_sequence(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_start"] = (-1, "", True)
        runner = _ScriptedRunner(plan)

        report = run_smoke(steps, runner=runner)

        assert report.outcome is SmokeOutcomeKind.TIMEOUT
        assert report.failing_step == "worktree_start"
        # Sequence stops at the timed-out step — subsequent steps must not run.
        assert "worktree_ready" not in runner.calls
        assert "worktree_teardown" not in runner.calls

    def test_first_failure_short_circuits_remaining_steps(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_provision"] = (1, "broken", False)
        runner = _ScriptedRunner(plan)

        run_smoke(steps, runner=runner)

        # Steps after worktree_provision must NOT execute on failure — a
        # green teardown cannot prove the rest of the sequence, and a
        # broken provision invalidates everything that follows.
        assert "worktree_start" not in runner.calls
        assert "worktree_ready" not in runner.calls
        assert "worktree_teardown" not in runner.calls
        assert "workspace_clean_all" not in runner.calls

    def test_unknown_step_name_falls_back_to_unknown_outcome(self) -> None:
        custom = [SmokeStep(name="unmapped_step", command=("noop",))]
        runner = _ScriptedRunner({"unmapped_step": (1, "boom", False)})

        report = run_smoke(custom, runner=runner)

        assert report.outcome is SmokeOutcomeKind.UNKNOWN
        assert report.failing_step == "unmapped_step"

    def test_runner_crash_recorded_as_failure(self) -> None:
        steps = [SmokeStep(name="worktree_provision", command=("noop",))]

        def boom(step: SmokeStep) -> StepResult:
            msg = "classifier denied"
            raise RuntimeError(msg)

        report = run_smoke(steps, runner=boom)

        assert report.outcome is SmokeOutcomeKind.PROVISION_FAILED
        assert report.failing_step == "worktree_provision"
        assert "classifier denied" in report.failing_step_stderr


class TestReportSummary:
    def test_pass_summary_includes_step_count(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        runner = _ScriptedRunner(_all_green(steps))
        report = run_smoke(steps, runner=runner)

        assert "PASS" in report_summary(report)
        assert str(len(steps)) in report_summary(report)

    def test_failure_summary_names_step_and_outcome(self) -> None:
        steps = default_steps(overlay="testoverlay", fixture_ticket_url="https://x/issues/1")
        plan = _all_green(steps)
        plan["worktree_provision"] = (1, "dslr alias missing", False)
        runner = _ScriptedRunner(plan)

        report = run_smoke(steps, runner=runner)
        summary = report_summary(report)

        assert "provision_failed" in summary
        assert "worktree_provision" in summary
        assert "dslr alias missing" in summary


class TestStepOutcomeKindCoverage:
    @pytest.mark.parametrize(
        ("step_name", "expected_kind"),
        [
            ("workspace_ticket", SmokeOutcomeKind.PROVISION_FAILED),
            ("env_show", SmokeOutcomeKind.PROVISION_FAILED),
            ("worktree_provision", SmokeOutcomeKind.PROVISION_FAILED),
            ("worktree_start", SmokeOutcomeKind.START_FAILED),
            ("worktree_ready", SmokeOutcomeKind.READY_FAILED),
            ("worktree_teardown", SmokeOutcomeKind.TEARDOWN_FAILED),
            ("workspace_clean_all", SmokeOutcomeKind.CLEAN_FAILED),
        ],
    )
    def test_every_default_step_has_an_outcome_mapping(self, step_name: str, expected_kind: SmokeOutcomeKind) -> None:
        assert STEP_OUTCOME_KIND[step_name] is expected_kind

    def test_empty_smoke_passes_trivially(self) -> None:
        report = run_smoke([], runner=_step_result)
        assert report.passed
        assert report.steps == []


def test_smoke_report_is_dataclass_with_evidence_trail() -> None:
    """The report carries the per-step evidence trail for the DM body."""
    report = SmokeReport()
    assert report.steps == []
    assert report.outcome is SmokeOutcomeKind.PASS
