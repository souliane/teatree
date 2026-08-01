"""``_check_dream_consolidation_blocked`` — the hard tier of the dream alarm (#3993).

The 48h ``_check_dream_staleness`` WARN is advisory and its verdict is deliberately
discarded, so a pass that ran nightly and failed a gate for 13 days never reddened
``t3 doctor check``. This is the escalation: a pass that once succeeded and has not
for ``CRITICAL_STALE_MULTIPLE`` staleness windows hard-FAILs and gates the exit code.
"""

import datetime as dt
import io
from contextlib import redirect_stdout
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.cli.doctor.checks_loop import _check_dream_consolidation_blocked
from teatree.core.models import DreamRunMarker
from teatree.core.models.dream_run_marker import CRITICAL_STALE_MULTIPLE, STALE_THRESHOLD_HOURS

_BLOCKED_HOURS = STALE_THRESHOLD_HOURS * CRITICAL_STALE_MULTIPLE + 1


class DreamBlockedDoctorCheckTestCase(TestCase):
    def test_a_blocked_pass_hard_fails(self) -> None:
        DreamRunMarker.objects.mark_succeeded(timezone.now() - dt.timedelta(hours=_BLOCKED_HOURS))
        DreamRunMarker.objects.mark_attempted(timezone.now())
        assert _check_dream_consolidation_blocked() is False

    def test_merely_stale_stays_advisory(self) -> None:
        # One missed window is the WARN tier's business, not this one.
        DreamRunMarker.objects.mark_succeeded(timezone.now() - dt.timedelta(hours=STALE_THRESHOLD_HOURS + 1))
        assert _check_dream_consolidation_blocked() is True

    def test_bootstrap_never_reddens_a_fresh_install(self) -> None:
        assert _check_dream_consolidation_blocked() is True

    def test_a_recent_success_is_ok(self) -> None:
        DreamRunMarker.objects.mark_succeeded(timezone.now())
        assert _check_dream_consolidation_blocked() is True

    def test_the_failure_line_names_the_last_success_and_the_remedy(self) -> None:
        DreamRunMarker.objects.mark_succeeded(timezone.now() - dt.timedelta(hours=_BLOCKED_HOURS))
        buf = io.StringIO()
        with redirect_stdout(buf):
            _check_dream_consolidation_blocked()
        out = buf.getvalue()
        assert "FAIL" in out
        assert "t3 dream run" in out

    def test_a_crashed_read_degrades_to_ok(self) -> None:
        buf = io.StringIO()
        with (
            patch.object(DreamRunMarker.objects, "is_critically_stale", side_effect=RuntimeError("db offline")),
            redirect_stdout(buf),
        ):
            assert _check_dream_consolidation_blocked() is True
        assert "WARN" in buf.getvalue()


class DreamBlockedGatesTheDoctorExitCodeTestCase(TestCase):
    """Wired into the GATING set — a verdict discarded like the advisory one is inert."""

    def test_run_doctor_checks_folds_the_blocked_check_into_ok(self) -> None:
        assert "_check_dream_consolidation_blocked" in _calls_feeding_the_exit_code()

    def test_the_advisory_set_does_not_swallow_it(self) -> None:
        import inspect  # noqa: PLC0415 — deferred: only this structural assertion needs it

        from teatree.cli.doctor import app  # noqa: PLC0415 — deferred: heavy CLI import at call time

        assert "_check_dream_consolidation_blocked" not in inspect.getsource(app._run_daily_advisories)


def _calls_feeding_the_exit_code() -> set[str]:
    """Every check whose verdict is assigned into ``run_doctor_checks``' ``ok``.

    A check called as a bare statement is surfacing-only — its verdict is discarded and
    it can never redden the run — so naming the function is not enough; the assignment
    is what makes it a gate.
    """
    import ast  # noqa: PLC0415 — deferred: only this structural assertion needs it
    import inspect  # noqa: PLC0415 — deferred: only this structural assertion needs it
    import textwrap  # noqa: PLC0415 — deferred: only this structural assertion needs it

    from teatree.cli.doctor import app  # noqa: PLC0415 — deferred: heavy CLI import at call time

    tree = ast.parse(textwrap.dedent(inspect.getsource(app.run_doctor_checks)))
    return {
        call.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ok" for t in node.targets)
        for call in ast.walk(node.value)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }
