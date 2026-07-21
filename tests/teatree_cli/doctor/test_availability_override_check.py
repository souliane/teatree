"""``_check_availability_override_staleness`` — the `t3 doctor` stale-mode-override alarm (#3274, #61).

A no-expiry away-class mode override silently suppresses the colleague-facing loops
(and parks the self-pump for a pump-pausing mode) for as long as it sits — the
incident that motivated the finding left one active for ~30h. Post-merge the finding
keys on the DB ``ModeOverride.set_at`` + the resolved mode's intrinsic booleans. The
doctor surfaces a WARN naming the deferred loops; a fresh, bounded, or absent
override is silent (surfacing-only, never gates the exit code).
"""

import io
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta

from django.test import TestCase

from teatree.cli.doctor.checks_availability import (
    OverridePosture,
    _check_availability_override_staleness,
    stale_override_finding,
)
from teatree.core.models import Loop, Mode, ModeOverride


def _run() -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        _check_availability_override_staleness()
    return buf.getvalue()


def _backdate_override(hours: int) -> None:
    ModeOverride.objects.all().update(set_at=datetime.now(tz=UTC) - timedelta(hours=hours))


class AvailabilityOverrideDoctorCheckTestCase(TestCase):
    def _seed_review_loop(self) -> None:
        Loop.objects.get_or_create(
            name="review",
            defaults={"delay_seconds": 60, "colleague_facing": True, "script": "src/teatree/loops/review/loop.py"},
        )

    def test_old_no_expiry_deferring_override_warns_and_names_loops(self) -> None:
        self._seed_review_loop()
        Mode.objects.update_or_create(name="unattended", defaults={"entries": {}, "defers_questions": True})
        ModeOverride.objects.set_override("unattended")
        _backdate_override(30)
        out = _run()
        assert "WARN" in out
        assert "unattended" in out
        assert "review" in out

    def test_no_override_is_silent(self) -> None:
        assert _run() == ""

    def test_recent_override_is_silent(self) -> None:
        Mode.objects.update_or_create(name="unattended", defaults={"entries": {}, "defers_questions": True})
        ModeOverride.objects.set_override("unattended")
        _backdate_override(1)
        assert _run() == ""

    def test_present_class_override_is_silent(self) -> None:
        Mode.objects.update_or_create(name="engaged", defaults={"entries": {}, "defers_questions": False})
        ModeOverride.objects.set_override("engaged")
        _backdate_override(30)
        assert _run() == ""


class TestStaleOverrideFinding:
    """#3274: `t3 doctor` flags a no-expiry deferring override that has outlived the threshold."""

    _NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    _LOOPS = ("review", "followup")

    def _finding(
        self,
        *,
        defers: bool = True,
        pauses: bool = False,
        has_expiry: bool = False,
        set_at: datetime | None,
        mode_name: str = "unattended",
    ) -> str | None:
        posture = OverridePosture(
            mode_name=mode_name,
            defers_questions=defers,
            pauses_self_pump=pauses,
            has_expiry=has_expiry,
            set_at=set_at,
        )
        return stale_override_finding(posture, now=self._NOW, colleague_facing_loops=self._LOOPS)

    def test_old_no_expiry_deferring_is_flagged(self) -> None:
        msg = self._finding(set_at=self._NOW - timedelta(hours=30))
        assert msg is not None
        assert "unattended" in msg
        assert "followup, review" in msg
        # The remediation points at the real mode-set command (``t3 mode`` is not a
        # typer leaf; the mode IS the loop preset post-merge).
        assert "t3 loop preset auto" in msg

    def test_old_no_expiry_pausing_notes_self_pump_park(self) -> None:
        msg = self._finding(defers=True, pauses=True, mode_name="offline", set_at=self._NOW - timedelta(hours=30))
        assert msg is not None
        assert "self-pump" in msg

    def test_recent_override_is_not_flagged(self) -> None:
        assert self._finding(set_at=self._NOW - timedelta(hours=1)) is None

    def test_bounded_override_is_not_flagged(self) -> None:
        assert self._finding(has_expiry=True, set_at=self._NOW - timedelta(hours=30)) is None

    def test_present_class_override_is_not_flagged(self) -> None:
        assert self._finding(defers=False, set_at=self._NOW - timedelta(hours=30)) is None

    def test_missing_set_at_is_not_flagged(self) -> None:
        assert self._finding(set_at=None) is None
