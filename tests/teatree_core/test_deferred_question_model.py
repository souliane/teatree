"""Tests for the :class:`DeferredQuestion` model (#58, BLUEPRINT §17.1 invariant 9).

Mirrors the ``OnBehalfApproval`` test layout 1:1: every contract clause
the model promises in its docstring is asserted here (guarded factory,
single-use consume, scope of queryset, audit row).
"""

import pytest

from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit, DeferredQuestionError
from teatree.instance_id import instance_id

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestDeferredQuestionRecord:
    def test_record_creates_a_pending_row(self) -> None:
        row = DeferredQuestion.record(
            "Should I proceed with the refactor?",
            options_json='[{"label": "yes"}, {"label": "no"}]',
            session_id="sess-1",
            tool_use_id="toolu_1",
        )
        assert row.pk is not None
        assert row.is_pending is True
        assert row.status == DeferredQuestion.STATUS_PENDING
        assert row.answered_at is None
        assert row.dismissed_at is None
        assert row.question == "Should I proceed with the refactor?"
        assert row.session_id == "sess-1"
        assert row.tool_use_id == "toolu_1"

    def test_record_strips_and_requires_question(self) -> None:
        with pytest.raises(DeferredQuestionError, match="question is required"):
            DeferredQuestion.record("   ")

    def test_record_keeps_optional_fields_default_empty(self) -> None:
        row = DeferredQuestion.record("Just the text.")
        assert row.options_json == ""
        assert row.session_id == ""
        assert row.tool_use_id == ""


class TestDeferredQuestionPending:
    def test_pending_returns_only_unresolved_rows_oldest_first(self) -> None:
        first = DeferredQuestion.record("first?")
        second = DeferredQuestion.record("second?")
        third = DeferredQuestion.record("third?")
        DeferredQuestion.consume(second.pk, answer="ok")

        pending = list(DeferredQuestion.pending())
        assert [r.pk for r in pending] == [first.pk, third.pk]


class TestDeferredQuestionConsume:
    def test_consume_with_answer_marks_answered_and_returns_row(self) -> None:
        row = DeferredQuestion.record("ship?")
        consumed = DeferredQuestion.consume(row.pk, answer="yes")
        assert consumed is not None
        assert consumed.answered_at is not None
        assert consumed.answer_text == "yes"
        assert consumed.status == DeferredQuestion.STATUS_ANSWERED
        assert consumed.is_pending is False

    def test_consume_with_dismiss_marks_dismissed(self) -> None:
        row = DeferredQuestion.record("ship?")
        consumed = DeferredQuestion.consume(row.pk, dismissed_reason="no longer relevant")
        assert consumed is not None
        assert consumed.dismissed_at is not None
        assert consumed.dismissed_reason == "no longer relevant"
        assert consumed.status == DeferredQuestion.STATUS_DISMISSED

    def test_consume_is_single_use(self) -> None:
        row = DeferredQuestion.record("ship?")
        assert DeferredQuestion.consume(row.pk, answer="yes") is not None
        assert DeferredQuestion.consume(row.pk, answer="yes again") is None

    def test_consume_returns_none_for_unknown_id(self) -> None:
        assert DeferredQuestion.consume(999_999, answer="x") is None

    def test_consume_requires_exactly_one_resolution_kind(self) -> None:
        row = DeferredQuestion.record("ship?")
        with pytest.raises(DeferredQuestionError, match="exactly one"):
            DeferredQuestion.consume(row.pk, answer="", dismissed_reason="")
        with pytest.raises(DeferredQuestionError, match="exactly one"):
            DeferredQuestion.consume(row.pk, answer="a", dismissed_reason="b")


class TestDeferredQuestionAuditRow:
    def test_audit_records_who_what_when(self) -> None:
        row = DeferredQuestion.record("ship?")
        consumed = DeferredQuestion.consume(row.pk, answer="yes")
        assert consumed is not None
        audit = DeferredQuestionAudit.objects.create(
            question=consumed,
            action="answered",
            answer_text="yes",
            resolver_id="souliane",
        )
        assert audit.resolver_id == "souliane"
        assert audit.action == "answered"
        assert audit.answer_text == "yes"


class TestStableNotifyRef:
    """Outward-notification idempotency keys derive from a stable identity, never the local pk.

    Fleet-safety Stage 1: two teatree instances keep independent SQLite, so a
    key built from a local autoincrement pk shifts between them. The resurface /
    mirror drains key their ``BotPing`` idempotency on ``stable_notify_ref``.
    """

    def test_key_is_stable_across_two_instances_with_different_local_pks(self) -> None:
        # Model the same logical question captured under two instances: identical
        # harness tool_use_id, but the local DBs assign different autoincrement
        # pks. A pk-derived key would differ between them (double-post / false
        # dedup); the stable ref must be identical.
        first = DeferredQuestion.record("Approve the merge?", tool_use_id="toolu_shared")
        second = DeferredQuestion.record("Approve the merge?", tool_use_id="toolu_shared")

        assert first.pk != second.pk
        assert first.stable_notify_ref == second.stable_notify_ref
        assert first.stable_notify_ref == "toolu_shared"

    def test_falls_back_to_instance_qualified_pk_never_bare_pk(self) -> None:
        row = DeferredQuestion.record("No harness id here")
        assert row.tool_use_id == ""
        assert row.stable_notify_ref == f"{instance_id()}:{row.pk}"
        assert row.stable_notify_ref != str(row.pk)


class TestStrRepr:
    def test_question_str(self) -> None:
        row = DeferredQuestion.record("Will it scale?")
        assert "deferred-question" in str(row)
        assert "pending" in str(row)

    def test_audit_str(self) -> None:
        row = DeferredQuestion.record("Will it scale?")
        consumed = DeferredQuestion.consume(row.pk, answer="yes")
        assert consumed is not None
        audit = DeferredQuestionAudit.objects.create(
            question=consumed,
            action="answered",
            answer_text="yes",
            resolver_id="souliane",
        )
        assert "deferred-question-audit" in str(audit)
        assert "souliane" in str(audit)
