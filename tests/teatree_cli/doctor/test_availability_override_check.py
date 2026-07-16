"""``_check_availability_override_staleness`` — the `t3 doctor` stale-override alarm (#3274).

A no-expiry ``away`` / ``autonomous_away`` availability override silently
suppresses the colleague-facing loops (and pauses the self-pump under
holiday-``away``) for as long as it sits — the incident that motivated the
finding left one active for ~30h. The doctor surfaces a WARN naming the deferred
loops. Surfacing-only (never gates the exit code); a fresh, bounded, or absent
override is silent. The pure ``override_set_at`` / ``stale_override_finding``
helpers the check builds on live in the same module and are unit-tested here too.
"""

import io
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.cli.doctor import checks_availability
from teatree.cli.doctor.checks_availability import (
    _check_availability_override_staleness,
    override_set_at,
    stale_override_finding,
)
from teatree.core import availability
from teatree.core.availability import MODE_AUTONOMOUS_AWAY, MODE_AWAY, MODE_PRESENT, Override, write_override
from teatree.core.models import Loop


def _run() -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        _check_availability_override_staleness()
    return buf.getvalue()


@pytest.fixture
def override_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "availability_override.json"
    monkeypatch.setattr("teatree.core.availability.override_path", lambda: target)
    return target


class AvailabilityOverrideDoctorCheckTestCase(TestCase):
    def test_old_no_expiry_override_warns_and_names_loops(self) -> None:
        # `name="review"` may collide with the initial migration's seeded default
        # loop (`_seed_default_loops` already creates it `colleague_facing=True`)
        # — `get_or_create` stays correct whether or not that seed row exists.
        Loop.objects.get_or_create(
            name="review",
            defaults={
                "delay_seconds": 60,
                "colleague_facing": True,
                "script": "src/teatree/loops/review/loop.py",
            },
        )
        old = datetime.now(tz=UTC) - timedelta(hours=30)
        with (
            patch.object(availability, "load_override", return_value=Override(mode=MODE_AUTONOMOUS_AWAY, until=None)),
            patch.object(checks_availability, "override_set_at", return_value=old),
        ):
            out = _run()
        assert "WARN" in out
        assert "autonomous_away" in out
        assert "review" in out

    def test_no_override_is_silent(self) -> None:
        with patch.object(availability, "load_override", return_value=None):
            assert _run() == ""

    def test_recent_override_is_silent(self) -> None:
        recent = datetime.now(tz=UTC) - timedelta(hours=1)
        with (
            patch.object(availability, "load_override", return_value=Override(mode=MODE_AUTONOMOUS_AWAY, until=None)),
            patch.object(checks_availability, "override_set_at", return_value=recent),
        ):
            assert _run() == ""

    def test_crash_is_swallowed_with_warn(self) -> None:
        with patch.object(availability, "load_override", side_effect=RuntimeError("disk gone")):
            out = _run()
        assert "WARN" in out
        assert "RuntimeError" in out


class TestOverrideSetAt:
    """`override_set_at` reads the override file mtime — the "how long active" signal (#3274)."""

    def test_returns_the_file_mtime_when_present(self, override_file: Path) -> None:
        write_override(MODE_AUTONOMOUS_AWAY)
        set_at = override_set_at()
        assert set_at is not None
        assert abs((datetime.now(tz=UTC) - set_at).total_seconds()) < 60

    def test_returns_none_when_absent(self, override_file: Path) -> None:
        assert override_set_at() is None


class TestStaleOverrideFinding:
    """#3274: `t3 doctor` flags a no-expiry deferring override that has outlived the threshold."""

    _NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    _LOOPS = ("review", "followup")

    def _finding(self, override: Override | None, *, set_at: datetime | None) -> str | None:
        return stale_override_finding(
            override=override,
            set_at=set_at,
            now=self._NOW,
            colleague_facing_loops=self._LOOPS,
        )

    def test_old_no_expiry_autonomous_away_is_flagged(self) -> None:
        override = Override(mode=MODE_AUTONOMOUS_AWAY, until=None)
        msg = self._finding(override, set_at=self._NOW - timedelta(hours=30))
        assert msg is not None
        assert "autonomous_away" in msg
        assert "followup, review" in msg
        assert "t3 teatree availability auto" in msg

    def test_old_no_expiry_away_notes_self_pump_pause(self) -> None:
        override = Override(mode=MODE_AWAY, until=None)
        msg = self._finding(override, set_at=self._NOW - timedelta(hours=30))
        assert msg is not None
        assert "self-pump" in msg

    def test_recent_override_is_not_flagged(self) -> None:
        override = Override(mode=MODE_AUTONOMOUS_AWAY, until=None)
        assert self._finding(override, set_at=self._NOW - timedelta(hours=1)) is None

    def test_bounded_override_is_not_flagged(self) -> None:
        override = Override(mode=MODE_AUTONOMOUS_AWAY, until=self._NOW + timedelta(hours=48))
        assert self._finding(override, set_at=self._NOW - timedelta(hours=30)) is None

    def test_present_override_is_not_flagged(self) -> None:
        override = Override(mode=MODE_PRESENT, until=None)
        assert self._finding(override, set_at=self._NOW - timedelta(hours=30)) is None

    def test_no_override_is_not_flagged(self) -> None:
        assert self._finding(None, set_at=self._NOW - timedelta(hours=30)) is None

    def test_missing_set_at_is_not_flagged(self) -> None:
        override = Override(mode=MODE_AUTONOMOUS_AWAY, until=None)
        assert self._finding(override, set_at=None) is None
