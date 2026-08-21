"""The finding vocabulary the reconciliation checks share.

Its own module so the check families (control-DB invariants in
:mod:`~teatree.cli.doctor.checks_reconciliation`, forge-read outcomes in
:mod:`~teatree.cli.doctor.checks_external_outcomes`) can both speak it without
importing each other. Django-free at import, like every ``checks_*`` sibling —
the doctor CLI group loads before ``ensure_django()``.

The three levels are not a severity scale, they are three different claims:
``ok`` asserts the invariant holds, ``alarm`` asserts it is violated, and
``degraded`` asserts nothing at all because the read did not produce an answer.
Collapsing ``degraded`` into either of the others is the failure this vocabulary
exists to prevent — an unread measurement is not a healthy one.
"""

import dataclasses
import datetime as dt


class Level:
    OK = "ok"
    ALARM = "alarm"
    DEGRADED = "degraded"


@dataclasses.dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    """One end-to-end invariant's verdict: healthy, violated (DM'd), or unreadable."""

    check_id: str
    level: str
    message: str

    @property
    def is_alarm(self) -> bool:
        return self.level == Level.ALARM


def _ok(check_id: str, message: str = "") -> ReconciliationFinding:
    return ReconciliationFinding(check_id=check_id, level=Level.OK, message=message)


def _alarm(check_id: str, message: str) -> ReconciliationFinding:
    return ReconciliationFinding(check_id=check_id, level=Level.ALARM, message=message)


def _degraded(check_id: str, exc: Exception) -> ReconciliationFinding:
    return ReconciliationFinding(
        check_id=check_id,
        level=Level.DEGRADED,
        message=f"reconciliation check `{check_id}` read crashed: {exc.__class__.__name__}: {exc}",
    )


def _unavailable(check_id: str, reason: str) -> ReconciliationFinding:
    """The read completed no measurement. Never ``_ok``: unmeasured is not healthy."""
    return ReconciliationFinding(
        check_id=check_id,
        level=Level.DEGRADED,
        message=f"reconciliation check `{check_id}` could not measure external output: {reason}",
    )


def _now(now: dt.datetime | None) -> dt.datetime:
    if now is not None:
        return now
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    return timezone.now()
