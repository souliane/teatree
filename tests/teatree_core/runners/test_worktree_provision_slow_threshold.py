"""A provision's slow-run alert threshold is the setting's declared default whenever the setting is unusable."""

from unittest.mock import MagicMock, patch

from teatree.core.provision.provision_report import ProvisionReport, StepResult
from teatree.core.runners.worktree_provision import WorktreeProvisionRunner

_RUNNER = "teatree.core.runners.worktree_provision"


def _report_taking(seconds: float) -> ProvisionReport:
    return ProvisionReport(steps=[StepResult(name="migrate", success=True, duration=seconds)])


def _persist(report: ProvisionReport, *, configured: object, declared: int) -> MagicMock:
    runner = WorktreeProvisionRunner(MagicMock(), overlay=MagicMock())
    with (
        patch(f"{_RUNNER}.get_effective_settings", return_value=MagicMock(provision_slow_threshold_seconds=configured)),
        patch(f"{_RUNNER}.UserSettings", return_value=MagicMock(provision_slow_threshold_seconds=declared)),
        patch(f"{_RUNNER}.alert_provision_user") as alert,
    ):
        runner._persist_report(report)
    return alert


def test_an_unreadable_threshold_alerts_past_the_declared_default() -> None:
    alert = _persist(_report_taking(50), configured="never", declared=45)

    assert alert.call_args.kwargs["detail"].endswith("exceeded the 45s threshold")


def test_an_unreadable_threshold_stays_quiet_below_the_declared_default() -> None:
    assert not _persist(_report_taking(40), configured="never", declared=45).called


def test_a_readable_threshold_wins_over_the_declared_default() -> None:
    assert not _persist(_report_taking(50), configured=60, declared=45).called
