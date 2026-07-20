"""Tests for the :class:`DeferredQuestion` model (#58, BLUEPRINT §17.1 invariant 9).

Mirrors the ``OnBehalfApproval`` test layout 1:1: every contract clause
the model promises in its docstring is asserted here (guarded factory,
single-use consume, scope of queryset, audit row).
"""

import pytest

from teatree.core.models.deferred_question import (
    DeferredQuestion,
    DeferredQuestionAudit,
    DeferredQuestionError,
    is_tool_lack_selfreport,
    question_fingerprint,
)
from teatree.instance_id import instance_id

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


class TestDedupeMarker:
    def test_repeat_marker_collapses_to_one_pending_row(self) -> None:
        first = DeferredQuestion.record("stall on ticket 1", dedupe_marker="repair-stall:1:coding")
        second = DeferredQuestion.record("stall on ticket 1", dedupe_marker="repair-stall:1:coding")
        assert first.pk == second.pk
        assert DeferredQuestion.objects.filter(dedupe_marker="repair-stall:1:coding").count() == 1

    def test_eight_identical_reason_clones_collapse_via_fingerprint(self) -> None:
        marker = f"needs-input:{question_fingerprint('I lack the tools to review this PR')}"
        for _ in range(8):
            DeferredQuestion.record("I lack the tools to review this PR", dedupe_marker=marker)
        assert DeferredQuestion.pending().count() == 1

    def test_fingerprint_ignores_whitespace_and_case(self) -> None:
        assert question_fingerprint("I  lack   TOOLS ") == question_fingerprint("i lack tools")

    def test_distinct_markers_do_not_collapse(self) -> None:
        DeferredQuestion.record("q", dedupe_marker="a")
        DeferredQuestion.record("q", dedupe_marker="b")
        assert DeferredQuestion.pending().count() == 2

    def test_empty_marker_never_dedupes(self) -> None:
        DeferredQuestion.record("q")
        DeferredQuestion.record("q")
        assert DeferredQuestion.pending().count() == 2

    def test_resolved_marker_row_does_not_block_a_new_record(self) -> None:
        first = DeferredQuestion.record("stall", dedupe_marker="m")
        DeferredQuestion.consume(first.pk, answer="handled")
        second = DeferredQuestion.record("stall again", dedupe_marker="m")
        assert second.pk != first.pk


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


class TestDeferredQuestionAudience:
    """Audience separates owner questions from the box's internal escalations (Phase 2)."""

    def test_record_defaults_to_owner_audience(self) -> None:
        row = DeferredQuestion.record("Ship it?")
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION

    def test_record_accepts_internal_audience(self) -> None:
        row = DeferredQuestion.record(
            "Repair-loop stall on ticket 1",
            audience=DeferredQuestion.Audience.INTERNAL,
        )
        assert row.audience == DeferredQuestion.Audience.INTERNAL

    def test_unmirrored_pending_excludes_internal_rows(self) -> None:
        owner = DeferredQuestion.record("Owner decision?")
        DeferredQuestion.record("internal stall", audience=DeferredQuestion.Audience.INTERNAL)
        unmirrored = list(DeferredQuestion.unmirrored_pending())
        assert [r.pk for r in unmirrored] == [owner.pk]


class TestToolLackSelfReport:
    """An agent's own "I lack the tools to proceed" report is a dispatch fault (INTERNAL)."""

    @pytest.mark.parametrize(
        "text",
        [
            # The verbatim leak: a scanning-news park that reached the owner's DM.
            (
                "This session lacks any shell/write tool (no Bash, no Write/Edit, no gh) needed to run "
                "`manage.py shell -c record_candidate`, dedupe-check via `gh issue list`, or post the Slack DM."
            ),
            "I have no shell to run the management command.",
            "This agent runs shell-denied, so it cannot file the issue.",
            "This must be picked up by a session with the standard toolset.",
            "I need a session with the tools to complete this.",
            "Missing the gh CLI, so I can't open the PR.",
        ],
    )
    def test_tool_lack_phrasings_are_classified(self, text: str) -> None:
        assert is_tool_lack_selfreport(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Should I merge PR #7 or wait for the release branch?",
            "The design has two viable approaches — which do you prefer?",
            "I don't have enough context about the rollout plan to continue.",
            "Should I write the migration now or in a follow-up?",
        ],
    )
    def test_genuine_owner_questions_are_not_classified(self, text: str) -> None:
        assert is_tool_lack_selfreport(text) is False

    def test_classification_ignores_case_and_whitespace(self) -> None:
        assert is_tool_lack_selfreport("  This  session  LACKS  any  SHELL  tool. ") is True


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
