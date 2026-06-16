"""``record_firing`` increments ``action_count`` via a DB-side F-expression.

The pre-fix code used an in-memory read-modify-write on an existing row:
``firing.action_count += 1; firing.save()``.  Two concurrent ticks on the
same ``(detector, dedup_key)`` both read the same count before either
commits, each add 1 in Python, and the last writer clobbers the first —
classic lost-update that causes ``action_count`` to under-count.

The fix replaces the in-memory increment with
``SelfImproveFiring.objects.filter(pk=...).update(action_count=F(...)+1, ...)``
which pushes the entire mutation inside a single ``UPDATE … SET action_count =
action_count + 1 …`` evaluated atomically by the DB.
"""

from unittest.mock import patch

import pytest

from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.loop.self_improve.detectors.base import DetectorReport
from teatree.loop.self_improve.persistence import record_firing

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _report(state_hash: str = "h1") -> DetectorReport:
    return DetectorReport(
        detector="test_detector",
        dedup_key="test_detector::key",
        state_hash=state_hash,
        severity="warn",
        max_rung="statusline",
        summary="test",
        payload={},
        auto_fix=False,
    )


class TestRecordFiringAtomicIncrement:
    def test_first_call_creates_row_with_count_one(self) -> None:
        record_firing(_report(), action="statusline")
        assert SelfImproveFiring.objects.get().action_count == 1

    def test_sequential_calls_accumulate_count(self) -> None:
        record_firing(_report(state_hash="h1"), action="log")
        record_firing(_report(state_hash="h2"), action="statusline")
        record_firing(_report(state_hash="h3"), action="statusline")
        assert SelfImproveFiring.objects.get().action_count == 3

    def test_existing_row_uses_filter_update_not_save(self) -> None:
        """The existing-row branch must use ``.update()`` (F-expression), not ``.save()``.

        This is the structural RED-then-GREEN check: the pre-fix code calls
        ``firing.save(…)`` on an in-memory instance where ``firing.action_count``
        was incremented in Python — a classic lost-update under concurrency.
        The fix routes through ``SelfImproveFiring.objects.filter(pk=…).update(…)``
        so the increment is a single atomic SQL expression.

        We spy on ``SelfImproveFiring.save`` (the instance method).  A save call
        on the existing-row path means the buggy in-memory RMW is still in place.
        The fixed code must NOT call ``save`` for the update path.
        """
        # Seed the row so the second call hits the existing-row branch.
        record_firing(_report(state_hash="h1"), action="log")

        with patch.object(SelfImproveFiring, "save") as mock_save:
            record_firing(_report(state_hash="h2"), action="statusline")
            mock_save.assert_not_called()
