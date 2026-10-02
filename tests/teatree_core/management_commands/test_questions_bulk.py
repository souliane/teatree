"""Bulk resolution for ``t3 <overlay> questions dismiss`` / ``answer``.

A backlog is cleared by the class, not one question at a time. Each containerized
invocation costs its own round trip, so ninety single-id commands is hours of wall
clock — long enough that a sweep gets abandoned half-done, leaving the automated
halts that dominate the queue to bury the handful of questions a human must answer.

The property that makes a sweep usable is PARTIAL SUCCESS: ids race with the loop
that keeps filing new questions, so some in any batch are already resolved by the
time the command runs. One stale id must skip, not roll back the ids around it —
which is why each id is consumed in its own transaction rather than one wrapping
the batch.
"""

from io import StringIO

import pytest
from django.core.management import call_command

from teatree.core.models import DeferredQuestion

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _call(*args: str) -> tuple[str, str, int]:
    out, err, code = StringIO(), StringIO(), 0
    try:
        call_command(*args, stdout=out, stderr=err)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return out.getvalue(), err.getvalue(), code


def _pending(text: str) -> DeferredQuestion:
    # `status` is DERIVED from answered_at/dismissed_at, not stored — a pending row is
    # simply one where neither stamp is set, which is the model's default.
    return DeferredQuestion.objects.create(question=text)


class TestDismissTakesManyIds:
    def test_one_command_clears_the_whole_batch(self) -> None:
        rows = [_pending(f"q{i}") for i in range(3)]

        _out, _err, code = _call("questions", "dismiss", *[str(r.pk) for r in rows], "--reason", "swept")

        assert code == 0
        for row in rows:
            row.refresh_from_db()
            assert row.status == "dismissed"

    def test_a_single_id_still_works(self) -> None:
        # The pre-existing spelling must keep working — every caller uses it.
        row = _pending("only one")

        _out, _err, code = _call("questions", "dismiss", str(row.pk))

        assert code == 0
        row.refresh_from_db()
        assert row.status == "dismissed"

    def test_an_already_resolved_id_does_not_roll_back_the_others(self) -> None:
        # THE anti-vacuity case. A batch-wide transaction would undo the good ids
        # when it hit the stale one, and the sweep would silently make no progress.
        good_a, stale, good_b = _pending("a"), _pending("stale"), _pending("b")
        _call("questions", "dismiss", str(stale.pk))

        _out, err, code = _call("questions", "dismiss", str(good_a.pk), str(stale.pk), str(good_b.pk))

        assert code == 0, "one stale id must not fail a batch that resolved others"
        good_a.refresh_from_db()
        good_b.refresh_from_db()
        assert good_a.status == "dismissed"
        assert good_b.status == "dismissed"
        assert str(stale.pk) in err, "a skipped id must be NAMED, not silently dropped"

    def test_a_batch_that_resolves_nothing_is_a_failure(self) -> None:
        # Reporting success on a sweep that changed nothing is how a backlog looks
        # cleared while still being full.
        row = _pending("already gone")
        _call("questions", "dismiss", str(row.pk))

        _out, _err, code = _call("questions", "dismiss", str(row.pk))

        assert code == 1


class TestAnswerCarriesOneTextToManyQuestions:
    def test_also_resolves_the_extra_ids_with_the_same_text(self) -> None:
        # One decision routinely settles several clarifying questions the loop filed
        # per facet of the same ambiguous instruction.
        first, second, third = _pending("facet a"), _pending("facet b"), _pending("facet c")

        _out, _err, code = _call(
            "questions", "answer", str(first.pk), "one decision", "--also", str(second.pk), "--also", str(third.pk)
        )

        assert code == 0
        for row in (first, second, third):
            row.refresh_from_db()
            assert row.status == "answered"

    def test_the_positional_spelling_is_unchanged(self) -> None:
        row = _pending("solo")

        _out, _err, code = _call("questions", "answer", str(row.pk), "the answer")

        assert code == 0
        row.refresh_from_db()
        assert row.status == "answered"

    def test_empty_text_is_still_refused_before_anything_is_consumed(self) -> None:
        row = _pending("untouched")

        _out, _err, code = _call("questions", "answer", str(row.pk), "   ")

        assert code == 2
        row.refresh_from_db()
        assert row.status == "pending"
