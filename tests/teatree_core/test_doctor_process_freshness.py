"""``t3 doctor check`` FAILs on a stale role by READING its record, never by measuring itself (#4390).

The watchdog runs the doctor through ``docker compose exec``, i.e. in a fresh process
whose own snapshot is current by construction. #4390 measured the consequence: a probe
that spawns a new interpreter reports HEALTHY while the live worker is discarding every
result it completes, and that reading is what made the incident read as "does not
reproduce". So the check reads the record the stale process published.

``test_the_check_does_not_measure_its_own_process`` is the control for that: it asserts
this process's OWN freshness is not BEHIND in the very state where the check returns
False. A self-measuring implementation passes every other case here and fails that one.
"""

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import django.test
import pytest

from teatree.core.gates.schema_guard import doctor_check_process_code_freshness
from teatree.core.process_freshness import RECORD_PREFIX, RECORD_SUFFIX, FreshnessVerdict, read_process_freshness

_LOADED = "0068_the_head_this_process_booted_with"
_APPLIED = "0071_the_head_the_db_has_now"
_STARTED = "2026-08-11T08:22:00+00:00"
_APPLIED_AT = "2026-08-11T17:31:00+00:00"


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("T3_DATA_DIR", str(tmp_path))
    return tmp_path


def _publish(data_dir: Path, *, role: str, verdict: str, at: datetime, pid: int = 4242) -> None:
    payload = {
        "role": role,
        "pid": pid,
        "process_started_at": _STARTED,
        "loaded_head": _LOADED,
        "applied_head": _APPLIED,
        "applied_at": _APPLIED_AT,
        "verdict": verdict,
        "at": at.isoformat(),
    }
    (data_dir / f"{RECORD_PREFIX}{role}-{pid}{RECORD_SUFFIX}").write_text(json.dumps(payload), encoding="utf-8")


def test_a_behind_record_fails_and_names_both_heads_and_both_timestamps(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _publish(data_dir, role="worker", verdict="behind", at=datetime.now(UTC))

    assert doctor_check_process_code_freshness() is False

    line = capsys.readouterr().out
    assert line.startswith("FAIL")
    assert "worker" in line
    assert _LOADED in line
    assert _APPLIED in line
    assert _APPLIED_AT in line
    assert _STARTED in line
    assert "restart that role's container once no task is claimed" in line


class TestTheCheckReadsRatherThanMeasures(django.test.TestCase):
    """The anti-trap control: this process is CURRENT and the check still FAILs."""

    def setUp(self) -> None:
        super().setUp()
        data_dir = tempfile.TemporaryDirectory(prefix="process-freshness-doctor-")
        self.addCleanup(data_dir.cleanup)
        self.data_dir = Path(data_dir.name)
        env = patch.dict(os.environ, {"T3_DATA_DIR": data_dir.name})
        env.start()
        self.addCleanup(env.stop)

    def test_a_behind_record_fails_even_though_this_process_is_current(self) -> None:
        _publish(self.data_dir, role="worker", verdict="behind", at=datetime.now(UTC))

        assert read_process_freshness().verdict is not FreshnessVerdict.BEHIND
        assert doctor_check_process_code_freshness() is False


def test_a_current_record_passes_silently(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _publish(data_dir, role="worker", verdict="current", at=datetime.now(UTC))

    assert doctor_check_process_code_freshness() is True
    assert "FAIL" not in capsys.readouterr().out


def test_no_record_yet_warns_and_passes(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Every box is in this state until each role has been restarted once — it must not page."""
    assert doctor_check_process_code_freshness() is True
    assert capsys.readouterr().out.startswith("WARN")


def test_a_record_older_than_the_trust_window_warns_and_passes(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _publish(data_dir, role="admin", verdict="current", at=datetime.now(UTC) - timedelta(hours=2))

    assert doctor_check_process_code_freshness() is True
    output = capsys.readouterr().out
    assert output.startswith("WARN")
    assert "may not be ticking" in output


def test_an_unreadable_record_is_skipped_rather_than_failing_the_box(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (data_dir / f"{RECORD_PREFIX}worker-1{RECORD_SUFFIX}").write_text("{ truncated", encoding="utf-8")

    assert doctor_check_process_code_freshness() is True
    assert "FAIL" not in capsys.readouterr().out
