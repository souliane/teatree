"""``_check_availability_override_staleness`` — the `t3 doctor` stale-override alarm (#3274).

A no-expiry ``away`` / ``autonomous_away`` availability override silently
suppresses the colleague-facing loops (and pauses the self-pump under
holiday-``away``) for as long as it sits — the incident that motivated the
finding left one active for ~30h. The doctor surfaces a WARN naming the deferred
loops. Surfacing-only (never gates the exit code); a fresh, bounded, or absent
override is silent.
"""

import io
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase

from teatree.cli.doctor.checks import _check_availability_override_staleness
from teatree.core import availability
from teatree.core.availability import MODE_AUTONOMOUS_AWAY, Override
from teatree.core.models import Loop


def _run() -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        _check_availability_override_staleness()
    return buf.getvalue()


class AvailabilityOverrideDoctorCheckTestCase(TestCase):
    def test_old_no_expiry_override_warns_and_names_loops(self) -> None:
        Loop.objects.create(
            name="review",
            delay_seconds=60,
            colleague_facing=True,
            script="src/teatree/loops/review/loop.py",
        )
        old = datetime.now(tz=UTC) - timedelta(hours=30)
        with (
            patch.object(availability, "load_override", return_value=Override(mode=MODE_AUTONOMOUS_AWAY, until=None)),
            patch.object(availability, "override_set_at", return_value=old),
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
            patch.object(availability, "override_set_at", return_value=recent),
        ):
            assert _run() == ""

    def test_crash_is_swallowed_with_warn(self) -> None:
        with patch.object(availability, "load_override", side_effect=RuntimeError("disk gone")):
            out = _run()
        assert "WARN" in out
        assert "RuntimeError" in out
