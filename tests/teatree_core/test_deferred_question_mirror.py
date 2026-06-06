"""Mirror-field semantics on ``DeferredQuestion`` (#1174).

The Slack→Claude bridge unifies present-mode and away-mode capture on one
``DeferredQuestion`` row. These tests pin the new fields and methods that
let a Slack reply resolve exactly the live generation and never a stale
one: ``next_generation``, ``live_for_reply``, ``options_hash`` matching,
``apply_answer``, and ``mark_stale``.
"""

import hashlib
import json

import pytest
from django.utils import timezone

from teatree.core.models.deferred_question import DeferredQuestion

pytestmark = pytest.mark.django_db


def _options_hash(options: list[dict]) -> str:
    blob = json.dumps(options, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class TestNextGeneration:
    def test_first_generation_is_one(self) -> None:
        assert DeferredQuestion.next_generation(session_id="s1", run_id="r1") == 1

    def test_increments_within_scope(self) -> None:
        DeferredQuestion.record("q1", session_id="s1", run_id="r1", generation=1)
        assert DeferredQuestion.next_generation(session_id="s1", run_id="r1") == 2

    def test_scoped_per_run(self) -> None:
        DeferredQuestion.record("q1", session_id="s1", run_id="r1", generation=1)
        DeferredQuestion.record("q2", session_id="s1", run_id="r1", generation=2)
        assert DeferredQuestion.next_generation(session_id="s1", run_id="r2") == 1


class TestLiveForReply:
    def test_returns_highest_generation_pending_row_for_channel(self) -> None:
        old = DeferredQuestion.record(
            "old", session_id="s", run_id="r", generation=1, slack_channel="D1", slack_ts="100.0"
        )
        new = DeferredQuestion.record(
            "new", session_id="s", run_id="r", generation=2, slack_channel="D1", slack_ts="101.0"
        )
        live = DeferredQuestion.live_for_reply(channel="D1", after_ts="200.0")
        assert live is not None
        assert live.pk == new.pk
        assert old.pk != live.pk

    def test_reply_before_mirror_ts_does_not_bind(self) -> None:
        DeferredQuestion.record("q", session_id="s", run_id="r", generation=1, slack_channel="D1", slack_ts="500.0")
        assert DeferredQuestion.live_for_reply(channel="D1", after_ts="499.9") is None

    def test_other_channel_does_not_match(self) -> None:
        DeferredQuestion.record("q", session_id="s", run_id="r", generation=1, slack_channel="D1", slack_ts="100.0")
        assert DeferredQuestion.live_for_reply(channel="D2", after_ts="200.0") is None

    def test_resolved_row_is_not_live(self) -> None:
        row = DeferredQuestion.record(
            "q", session_id="s", run_id="r", generation=1, slack_channel="D1", slack_ts="100.0"
        )
        row.apply_answer("done", resolved_via="local")
        assert DeferredQuestion.live_for_reply(channel="D1", after_ts="200.0") is None


class TestApplyAnswer:
    def test_stamps_answer_and_resolved_via(self) -> None:
        row = DeferredQuestion.record("q", session_id="s", run_id="r", generation=1)
        applied = row.apply_answer("Yes", resolved_via="slack")
        assert applied is not None
        applied.refresh_from_db()
        assert applied.answer_text == "Yes"
        assert applied.resolved_via == "slack"
        assert applied.answered_at is not None
        assert applied.applied_at is None

    def test_second_apply_is_no_op(self) -> None:
        row = DeferredQuestion.record("q", session_id="s", run_id="r", generation=1)
        assert row.apply_answer("Yes", resolved_via="slack") is not None
        assert row.apply_answer("No", resolved_via="slack") is None


class TestMarkStale:
    def test_stamps_dismissed_and_resolved_via_stale(self) -> None:
        row = DeferredQuestion.record("q", session_id="s", run_id="r", generation=1)
        row.mark_stale("superseded by newer question")
        row.refresh_from_db()
        assert row.is_pending is False
        assert row.resolved_via == "stale"
        assert row.dismissed_reason == "superseded by newer question"
        assert row.audits.filter(action="dismissed").exists()


class TestOptionsHash:
    def test_record_stores_options_hash(self) -> None:
        options = [{"label": "Yes"}, {"label": "No"}]
        row = DeferredQuestion.record(
            "q",
            session_id="s",
            run_id="r",
            generation=1,
            options_json=json.dumps(options),
            options_hash=_options_hash(options),
        )
        assert row.options_hash == _options_hash(options)


class TestRecordBackwardCompatible:
    def test_legacy_record_call_still_works(self) -> None:
        row = DeferredQuestion.record("legacy", session_id="s", tool_use_id="t")
        assert row.generation == 0
        assert row.run_id == ""
        assert row.slack_ts == ""
        assert row.resolved_via == ""
        assert row.applied_at is None

    def test_applied_at_marks_delivery(self) -> None:
        row = DeferredQuestion.record("q", session_id="s", run_id="r", generation=1)
        row.apply_answer("Yes", resolved_via="slack")
        now = timezone.now()
        DeferredQuestion.objects.filter(pk=row.pk, applied_at__isnull=True).update(applied_at=now)
        row.refresh_from_db()
        assert row.applied_at is not None
