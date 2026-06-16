"""``t3 questions answer`` / ``dismiss`` write consume + audit atomically.

The pre-fix code called ``DeferredQuestion.consume(...)`` (which commits its
own inner ``transaction.atomic()``) and then created ``DeferredQuestionAudit``
in a separate, unwrapped statement.  A crash between the two commits produces
a resolved question with no audit row — contradicting the ``DeferredQuestionAudit``
docstring that claims the audit "lands together [with consume] or not at all".

The fix wraps both operations in a single outer ``transaction.atomic()`` in
each command handler so they land in a single transaction.

We verify atomicity structurally: after the outer transaction is in place,
the ``consume`` + audit-create must execute within the SAME database
transaction.  We probe this by patching ``DeferredQuestionAudit.objects.create``
to raise immediately after ``consume`` commits — on the FIXED code the outer
``atomic()`` rolls back the inner commit too, leaving no resolved row and no
audit; on the BUGGY code the resolved row persists with no audit.
"""

from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db(transaction=True)


def _make_question() -> DeferredQuestion:
    return DeferredQuestion.record("Should I proceed?")


class TestAnswerCommandAtomicity:
    def test_answer_writes_both_resolution_and_audit(self) -> None:
        row = _make_question()
        call_command("questions", "answer", row.pk, "yes", "--resolver", "test-user")
        row.refresh_from_db()
        assert row.answered_at is not None
        assert row.resolved_via == DeferredQuestion.ResolvedVia.LOCAL
        assert DeferredQuestionAudit.objects.filter(question=row, action="answered").count() == 1

    def test_answer_audit_failure_rolls_back_resolution(self) -> None:
        """If audit creation fails, the resolution must also be rolled back.

        Pre-fix: consume commits first (its own atomic), then audit creation
        fails → resolved row exists with no audit (split commits).
        Fixed: outer atomic wraps both → the whole transaction rolls back.
        """
        row = _make_question()
        pk = row.pk

        with (
            patch.object(
                DeferredQuestionAudit.objects,
                "create",
                side_effect=RuntimeError("audit write failed"),
            ),
            pytest.raises(RuntimeError, match="audit write failed"),
        ):
            call_command("questions", "answer", pk, "yes")

        # Fixed code: consume rolled back, question still pending.
        # Buggy code: question resolved but no audit row.
        refreshed = DeferredQuestion.objects.get(pk=pk)
        assert refreshed.is_pending, "resolution must be rolled back when audit fails"
        assert DeferredQuestionAudit.objects.filter(question_id=pk).count() == 0


class TestDismissCommandAtomicity:
    def test_dismiss_writes_both_resolution_and_audit(self) -> None:
        row = _make_question()
        call_command("questions", "dismiss", row.pk, "--reason", "stale")
        row.refresh_from_db()
        assert row.dismissed_at is not None
        assert row.resolved_via == DeferredQuestion.ResolvedVia.LOCAL
        assert DeferredQuestionAudit.objects.filter(question=row, action="dismissed").count() == 1

    def test_dismiss_audit_failure_rolls_back_resolution(self) -> None:
        """If audit creation fails, the dismissal must also be rolled back."""
        row = _make_question()
        pk = row.pk

        with (
            patch.object(
                DeferredQuestionAudit.objects,
                "create",
                side_effect=RuntimeError("audit write failed"),
            ),
            pytest.raises(RuntimeError, match="audit write failed"),
        ):
            call_command("questions", "dismiss", pk, "--reason", "stale")

        refreshed = DeferredQuestion.objects.get(pk=pk)
        assert refreshed.is_pending, "dismissal must be rolled back when audit fails"
        assert DeferredQuestionAudit.objects.filter(question_id=pk).count() == 0
