"""Asking the OWNER about their own merge request is not a post on their behalf.

``ask_mr_state`` is the surface the automation uses when it genuinely cannot
decide a merge request's state. It is the bot talking to its own operator about
the operator's own work — a different concern from posts made AS the user TO colleagues.
Routing this through the publish gate would silently swallow the question
whenever the operator has the (default) gate armed, which is exactly when they
most need to be asked.

So the load-bearing case here pins the question reaching the owner while the
publish gate is ARMED and the on-behalf receipt is OFF — and asserts inline that
the gate really would block a colleague-visible action under the same settings,
so a mis-staged environment cannot make the independence claim pass vacuously.

The other two properties are the anti-spam bounds R5 leans on: one open question
per merge request (dedupe on the canonical URL), and a hard per-tick cap so a
backlog of undecidable merge requests cannot arrive as a flood.
"""

import json
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core import notify as notify_module
from teatree.core.gates.review_request_guard import canonical_mr_url
from teatree.core.models import DeferredQuestion
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions
from teatree.core.on_behalf_gate_recorded import resolve_posture_verdict
from teatree.core.review import mr_state_question
from teatree.core.review.mr_state_question import (
    ask_mr_state,
    head_tag,
    mr_state_marker,
    observe_owner_question_creation,
    owner_answer_at_head,
)
from teatree.on_behalf_gate import OnBehalfVerdict
from tests.teatree_core._on_behalf_gate_helpers import seed_forbidding_posture

_MR = "https://git.example.com/acme/app/-/merge_requests/41"
_OTHER_MR = "https://git.example.com/acme/app/-/merge_requests/42"
_THIRD_MR = "https://git.example.com/acme/app/-/merge_requests/43"
_REASON = "the forge reports no head pipeline and the branch is behind target."
_HEAD = "a1b2c3d4e5f6" + "0" * 28
_NEW_HEAD = "f6e5d4c3b2a1" + "0" * 28


def _open_mr_state_questions() -> list[DeferredQuestion]:
    return list(DeferredQuestion.pending().filter(dedupe_marker__startswith="mr-state:"))


def _owner_dm_backend(*, ts: str = "1700000000.000000") -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D_OWNER"
    backend.post_message.return_value = {"ok": True, "ts": ts}
    backend.get_permalink.return_value = "https://acme.slack.com/archives/D_OWNER/p1700000000000000"
    return backend


class TestOneOpenQuestionPerMergeRequest(TestCase):
    def test_two_asks_for_the_same_merge_request_leave_exactly_one_pending_row(self) -> None:
        first = ask_mr_state(mr_url=_MR, reason=_REASON)
        second = ask_mr_state(mr_url=_MR, reason="still undecidable after a refetch.")

        assert first is not None
        assert second is not None
        assert second.pk == first.pk
        assert len(_open_mr_state_questions()) == 1

    def test_a_comment_permalink_dedupes_onto_the_merge_request_row(self) -> None:
        """The marker is the shared canonicaliser's output, so URL noise cannot fork it."""
        first = ask_mr_state(mr_url=_MR, reason=_REASON)
        second = ask_mr_state(mr_url=f"{_MR}#note_9", reason=_REASON)

        assert first is not None
        assert second is not None
        assert second.pk == first.pk
        assert len(_open_mr_state_questions()) == 1

    def test_a_trailing_slash_dedupes_onto_the_merge_request_row(self) -> None:
        first = ask_mr_state(mr_url=_MR, reason=_REASON)
        second = ask_mr_state(mr_url=f"{_MR}/", reason=_REASON)

        assert first is not None
        assert second is not None
        assert second.pk == first.pk

    def test_the_marker_is_the_shared_canonical_scope_verbatim(self) -> None:
        """Provably one string: the marker is the prefix plus the guard's own output.

        Anything the guard does not collapse is not collapsed here either — the
        point is a scope that cannot DRIFT from the review-request guard's, not a
        second, better canonicaliser that would silently disagree with it.
        """
        assert mr_state_marker(f"{_MR}#note_9") == f"mr-state:{canonical_mr_url(_MR)}"

    def test_distinct_merge_requests_get_distinct_rows(self) -> None:
        """The control for dedupe: collapsing every merge request would also pass a same-URL test."""
        first = ask_mr_state(mr_url=_MR, reason=_REASON)
        second = ask_mr_state(mr_url=_OTHER_MR, reason=_REASON)

        assert first is not None
        assert second is not None
        assert second.pk != first.pk
        assert len(_open_mr_state_questions()) == 2

    def test_the_question_names_the_merge_request_and_the_reason(self) -> None:
        row = ask_mr_state(mr_url=_MR, reason=_REASON)

        assert row is not None
        assert _MR in row.question
        assert _REASON in row.question

    def test_options_are_recorded_in_the_askuserquestion_shape(self) -> None:
        row = ask_mr_state(mr_url=_MR, reason=_REASON, options=("treat as ready", "hold", "close it"))

        assert row is not None
        assert [option["label"] for option in json.loads(row.options_json)] == [
            "treat as ready",
            "hold",
            "close it",
        ]


class TestPerTickCap(TestCase):
    def test_the_cap_refuses_the_next_distinct_merge_request(self) -> None:
        assert ask_mr_state(mr_url=_MR, reason=_REASON) is not None
        assert ask_mr_state(mr_url=_OTHER_MR, reason=_REASON) is not None
        assert ask_mr_state(mr_url=_THIRD_MR, reason=_REASON) is None
        assert len(_open_mr_state_questions()) == 2

    def test_a_raised_cap_admits_the_merge_request_the_lower_one_refused(self) -> None:
        """The control: without this the cap test would also pass on a reader that always refuses."""
        with patch.object(mr_state_question, "MAX_OPEN_QUESTIONS", 1):
            assert ask_mr_state(mr_url=_MR, reason=_REASON) is not None
            assert ask_mr_state(mr_url=_OTHER_MR, reason=_REASON) is None

        assert ask_mr_state(mr_url=_OTHER_MR, reason=_REASON) is not None

    def test_re_asking_an_already_open_merge_request_is_never_refused_by_the_cap(self) -> None:
        with patch.object(mr_state_question, "MAX_OPEN_QUESTIONS", 1):
            first = ask_mr_state(mr_url=_MR, reason=_REASON)

            second = ask_mr_state(mr_url=_MR, reason=_REASON)

        assert first is not None
        assert second is not None
        assert second.pk == first.pk

    def test_answering_a_question_frees_its_slot(self) -> None:
        with patch.object(mr_state_question, "MAX_OPEN_QUESTIONS", 1):
            first = ask_mr_state(mr_url=_MR, reason=_REASON)
            assert first is not None
            assert ask_mr_state(mr_url=_OTHER_MR, reason=_REASON) is None

            first.apply_answer("treat as ready", resolved_via=DeferredQuestion.ResolvedVia.LOCAL)

            assert ask_mr_state(mr_url=_OTHER_MR, reason=_REASON) is not None


class TestAHeadBoundQuestion(TestCase):
    def test_the_question_names_the_head_it_asks_about(self) -> None:
        row = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_HEAD)

        assert row is not None
        assert head_tag(_HEAD) in row.question
        assert head_tag(_HEAD) == f"[head {_HEAD[:12]}]"

    def test_a_question_about_a_new_head_supersedes_the_open_one(self) -> None:
        old = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_HEAD)
        new = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_NEW_HEAD)

        assert old is not None
        assert new is not None
        old.refresh_from_db()
        assert old.dismissed_at is not None
        assert [row.pk for row in _open_mr_state_questions()] == [new.pk]
        assert head_tag(_NEW_HEAD) in new.question

    def test_a_changed_blocker_at_the_same_head_supersedes_the_open_one(self) -> None:
        old = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_HEAD)
        new = ask_mr_state(mr_url=_MR, reason="the send was refused.", head_sha=_HEAD)

        assert old is not None
        assert new is not None
        assert new.pk != old.pk
        assert [row.pk for row in _open_mr_state_questions()] == [new.pk]

    def test_the_same_question_at_the_same_head_is_the_open_row(self) -> None:
        first = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_HEAD)
        second = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_HEAD)

        assert first is not None
        assert second is not None
        assert second.pk == first.pk

    def test_a_reason_already_asked_at_this_head_is_not_filed_again(self) -> None:
        ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_HEAD)
        second = ask_mr_state(mr_url=_MR, reason="the send was refused.", head_sha=_HEAD)
        third = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_HEAD)

        assert second is not None
        assert third is not None
        assert third.pk == second.pk
        assert DeferredQuestion.objects.filter(dedupe_marker=mr_state_marker(_MR)).count() == 2

    def test_a_replacement_keeps_its_slot_when_the_cap_is_full(self) -> None:
        old = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_HEAD)
        ask_mr_state(mr_url=_OTHER_MR, reason=_REASON)
        assert old is not None

        with patch.object(mr_state_question, "MAX_OPEN_QUESTIONS", 1):
            new = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_NEW_HEAD)

        assert new is not None
        assert new.pk != old.pk
        open_for_this_mr = [row.pk for row in _open_mr_state_questions() if row.dedupe_marker == mr_state_marker(_MR)]
        assert open_for_this_mr == [new.pk]

    def test_another_callers_untagged_question_is_never_superseded(self) -> None:
        untagged = ask_mr_state(mr_url=_MR, reason=_REASON)
        tagged = ask_mr_state(mr_url=_MR, reason="the send was refused.", head_sha=_HEAD)

        assert untagged is not None
        assert tagged is not None
        assert tagged.pk == untagged.pk
        assert [row.pk for row in _open_mr_state_questions()] == [untagged.pk]

    def test_an_untagged_ask_leaves_an_open_tagged_row_alone(self) -> None:
        tagged = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_HEAD)
        untagged = ask_mr_state(mr_url=_MR, reason="still undecidable.")

        assert tagged is not None
        assert untagged is not None
        assert untagged.pk == tagged.pk


class TestOwnerAnswerAtHead(TestCase):
    def test_the_answer_about_this_head_is_returned(self) -> None:
        row = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_HEAD)
        assert row is not None
        row.apply_answer("It is not ready yet", resolved_via=DeferredQuestion.ResolvedVia.LOCAL)

        answer = owner_answer_at_head(_MR, head_sha=_HEAD)

        assert answer is not None
        assert answer.answer_text == "It is not ready yet"

    def test_an_answer_about_another_head_is_not_returned(self) -> None:
        row = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_HEAD)
        assert row is not None
        row.apply_answer("It is not ready yet", resolved_via=DeferredQuestion.ResolvedVia.LOCAL)

        assert owner_answer_at_head(_MR, head_sha=_NEW_HEAD) is None

    def test_an_answer_to_an_untagged_question_is_ignored(self) -> None:
        row = ask_mr_state(mr_url=_MR, reason=_REASON)
        assert row is not None
        row.apply_answer("Post the review request", resolved_via=DeferredQuestion.ResolvedVia.LOCAL)

        assert owner_answer_at_head(_MR, head_sha=_HEAD) is None

    def test_a_head_the_owner_answered_about_is_not_asked_about_again(self) -> None:
        first = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_HEAD)
        assert first is not None
        first.apply_answer("I will ask in person", resolved_via=DeferredQuestion.ResolvedVia.LOCAL)

        again = ask_mr_state(mr_url=_MR, reason="the send was refused.", head_sha=_HEAD)

        assert again is None
        assert DeferredQuestion.objects.filter(dedupe_marker=mr_state_marker(_MR)).count() == 1

    def test_a_new_head_is_asked_about_after_an_answer_at_the_old_one(self) -> None:
        first = ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_HEAD)
        assert first is not None
        first.apply_answer("I will ask in person", resolved_via=DeferredQuestion.ResolvedVia.LOCAL)

        assert ask_mr_state(mr_url=_MR, reason=_REASON, head_sha=_NEW_HEAD) is not None


class TestOwnerQuestionObserver(TestCase):
    def test_a_new_question_notifies_the_observer_with_the_merge_request_url(self) -> None:
        observed: list[str] = []
        with observe_owner_question_creation(observed.append):
            row = ask_mr_state(mr_url=_MR, reason=_REASON)

        assert row is not None
        assert observed == [_MR]

    def test_a_duplicate_ask_does_not_notify_the_observer_again(self) -> None:
        """The idempotent-return branch (an already-open row) never reaches the observer call."""
        observed: list[str] = []
        with observe_owner_question_creation(observed.append):
            first = ask_mr_state(mr_url=_MR, reason=_REASON)
            second = ask_mr_state(mr_url=_MR, reason="still undecidable after a refetch.")

        assert first is not None
        assert second is not None
        assert second.pk == first.pk
        assert observed == [_MR]


class TestReachesTheOwnerIndependentOfThePublishGate(TestCase):
    def test_delivered_while_the_on_behalf_gate_is_armed(self) -> None:
        backend = _owner_dm_backend()

        seed_forbidding_posture()
        # Inline control: under this exact posture a colleague-visible action really is
        # blocked, so the delivery below is independence and not a gate that happened to
        # be disarmed.
        assert resolve_posture_verdict("post_comment") is OnBehalfVerdict.BLOCK

        row = ask_mr_state(mr_url=_MR, reason=_REASON)
        assert row is not None

        with patch.object(notify_module, "messaging_from_overlay", return_value=None):
            mirrored, total = drain_unmirrored_deferred_questions(user_id="U_OWNER", backend=backend)

        assert (mirrored, total) == (1, 1)
        backend.post_message.assert_called_once()
        assert _MR in backend.post_message.call_args.kwargs["text"]
        row.refresh_from_db()
        assert row.slack_channel == "D_OWNER"
        assert row.slack_ts == "1700000000.000000"

    def test_the_question_is_owner_audience_so_the_drain_never_filters_it_out(self) -> None:
        row = ask_mr_state(mr_url=_MR, reason=_REASON)

        assert row is not None
        assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION
