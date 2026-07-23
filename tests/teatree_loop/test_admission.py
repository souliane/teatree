"""Loop-side assembly of the admission governor (#3644).

The two properties that matter here are operational, not arithmetic: a refusal is
LOUD, and every degraded path yields "no opinion" rather than a denial — a governor
that wedges the factory when it cannot read its own signals is worse than none.
"""

import datetime as dt
import json
import logging
from pathlib import Path

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.admission_governor import AdmissionDecision, read_machine_signal, read_quota_signal
from teatree.core.models import Task
from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage, TokenHealthReading
from teatree.loop import admission
from tests.factories import TaskFactory


@pytest.mark.usefixtures("tmp_path")
class TestBrakeStatePersistence:
    def test_absent_sidecar_reads_un_braked(self, tmp_path: Path) -> None:
        assert admission.read_braked(statusline_path=tmp_path / "statusline.txt") is False

    def test_corrupt_sidecar_reads_un_braked(self, tmp_path: Path) -> None:
        (tmp_path / "tick-meta.json").write_text("{not json", encoding="utf-8")
        assert admission.read_braked(statusline_path=tmp_path / "statusline.txt") is False

    def test_round_trips_the_brake_state(self, tmp_path: Path) -> None:
        path = tmp_path / "statusline.txt"
        admission.write_braked(braked=True, statusline_path=path)
        assert admission.read_braked(statusline_path=path) is True

    def test_writing_preserves_every_other_tick_meta_key(self, tmp_path: Path) -> None:
        meta = tmp_path / "tick-meta.json"
        meta.write_text(json.dumps({"orchestrate_admit_budget": 4}), encoding="utf-8")
        admission.write_braked(braked=True, statusline_path=tmp_path / "statusline.txt")
        assert json.loads(meta.read_text(encoding="utf-8"))["orchestrate_admit_budget"] == 4

    def test_a_non_dict_sidecar_is_replaced_not_merged(self, tmp_path: Path) -> None:
        # A JSON array (or any non-object) at the sidecar path parses fine but is not a
        # merge target; it is discarded so the brake key still lands on a clean dict.
        meta = tmp_path / "tick-meta.json"
        meta.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        admission.write_braked(braked=True, statusline_path=tmp_path / "statusline.txt")
        assert json.loads(meta.read_text(encoding="utf-8")) == {admission.BRAKED_KEY: True}

    def test_an_unwritable_sidecar_is_logged_never_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A sidecar path that cannot be written as a file (here: it is a directory) must
        # never propagate — persisting the brake state is best-effort, and losing it costs
        # only one extra evaluation at the high watermark.
        (tmp_path / "tick-meta.json").mkdir()
        with caplog.at_level(logging.ERROR):
            admission.write_braked(braked=True, statusline_path=tmp_path / "statusline.txt")
        assert "brake state" in caplog.text


class TestGovernorVerdictDegradesToNoOpinion:
    def test_kill_switch_off_yields_no_opinion(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(admission, "governor_enabled", lambda: False)
        assert admission.governor_verdict(statusline_path=tmp_path / "statusline.txt") is None

    def test_a_raising_probe_yields_no_opinion_never_a_denial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(admission, "governor_enabled", lambda: True)
        monkeypatch.setattr(admission, "read_quota_signal", _boom)
        with caplog.at_level(logging.ERROR):
            assert admission.governor_verdict(statusline_path=tmp_path / "statusline.txt") is None
        assert "probe failed" in caplog.text


def _boom() -> None:
    message = "probe exploded"
    raise RuntimeError(message)


class TestRefusalIsVisible:
    def test_a_denial_is_logged_at_warning_with_its_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        denied = AdmissionDecision(admit=False, reason="weekly window spent", ceiling=1, braked=True)
        monkeypatch.setattr(admission, "governor_enabled", lambda: True)
        monkeypatch.setattr(admission, "read_quota_signal", lambda: None)
        monkeypatch.setattr(admission, "read_machine_signal", lambda: None)
        monkeypatch.setattr(admission, "read_yield_signal", lambda: None)
        monkeypatch.setattr(admission, "decide_admission", lambda **_: denied)
        with caplog.at_level(logging.WARNING):
            verdict = admission.governor_verdict(statusline_path=tmp_path / "statusline.txt")
        assert verdict is denied
        assert "weekly window spent" in caplog.text

    def test_the_denial_persists_the_brake_state_for_hysteresis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        denied = AdmissionDecision(admit=False, reason="load too high", ceiling=1, braked=True)
        monkeypatch.setattr(admission, "governor_enabled", lambda: True)
        monkeypatch.setattr(admission, "read_quota_signal", lambda: None)
        monkeypatch.setattr(admission, "read_machine_signal", lambda: None)
        monkeypatch.setattr(admission, "read_yield_signal", lambda: None)
        monkeypatch.setattr(admission, "decide_admission", lambda **_: denied)
        path = tmp_path / "statusline.txt"
        admission.governor_verdict(statusline_path=path)
        assert admission.read_braked(statusline_path=path) is True


class YieldSignalTestCase(TestCase):
    def test_counts_terminal_tasks_inside_the_window(self) -> None:
        for status in (Task.Status.COMPLETED, Task.Status.COMPLETED, Task.Status.FAILED):
            TaskFactory(status=status)
        signal = admission.read_yield_signal(timezone.now())
        assert (signal.completed, signal.failed) == (2, 1)

    def test_ignores_tasks_older_than_the_window(self) -> None:
        TaskFactory(status=Task.Status.COMPLETED)
        later = timezone.now() + admission.YIELD_WINDOW * 2
        assert admission.read_yield_signal(later).samples == 0


class QuotaSignalTestCase(TestCase):
    """The token signal reads the cache the routing selector already maintains."""

    def _record(self, *, u5h: float, u7d: float) -> None:
        now = timezone.now()
        AnthropicTokenUsage.objects.record(
            f"acct/{u5h}-{u7d}",
            TokenHealthReading(
                organization_id="org",
                utilization_5h=u5h,
                utilization_7d=u7d,
                status_5h="allowed",
                status_7d="allowed",
                reset_5h=now + dt.timedelta(hours=2),
                reset_7d=now + dt.timedelta(days=3),
            ),
            now=now,
        )

    def test_an_empty_cache_is_not_fresh(self) -> None:
        assert read_quota_signal(timezone.now()).fresh is False

    def test_reports_the_healthiest_account_and_its_weekly_runway(self) -> None:
        self._record(u5h=0.8, u7d=0.7)
        self._record(u5h=0.1, u7d=0.2)
        signal = read_quota_signal(timezone.now())
        assert signal.fresh
        assert signal.weekly_utilization == pytest.approx(0.2)
        assert signal.all_accounts_exhausted is False
        assert signal.seconds_to_weekly_reset == pytest.approx(3 * 24 * 3600, rel=1e-3)

    def test_every_account_over_the_limit_is_reported_exhausted(self) -> None:
        self._record(u5h=0.99, u7d=0.999)
        assert read_quota_signal(timezone.now()).all_accounts_exhausted is True


class TestMachineSignal:
    def test_reads_the_live_load_and_core_count(self) -> None:
        signal = read_machine_signal()
        assert signal.cores >= 1
        assert signal.load1 >= 0.0
        assert signal.ram_available_gb is None

    def test_carries_an_injected_ram_reading(self) -> None:
        assert read_machine_signal(ram_available_gb=12.5).ram_available_gb == pytest.approx(12.5)
