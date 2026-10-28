"""A tap on an owner-question button answers that question and closes its card in place (#4990).

One committed round trip: post a card through the first-post drain to a recording Slack client, then tap it.
"""

import contextlib
import datetime as dt
from typing import Any
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.db import OperationalError
from django.test import TestCase

from teatree.core import notify as notify_module
from teatree.core.models import DeferredQuestion, OperatorIdentity, Session, Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestionAudit
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions, reask_escalated_questions
from teatree.loop.question_binding import answer_from_click
from tests._owner_channel import OWNER_CARD, OWNER_DECISION
from tests.factories import planned_ticket

_OWNER = "U0DEMOOWNER"
_CHANNEL = "D0DEMOOWNER"
_ROOT_TS = "1800000000.000100"


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.user_id = _OWNER
    backend.open_dm.return_value = _CHANNEL
    backend.post_message.return_value = {"ok": True, "ts": _ROOT_TS}
    backend.update_message.return_value = {"ok": True}
    backend.get_permalink.return_value = "https://acme.slack.example/archives/D0DEMOOWNER/p1800000000000100"
    return backend


def _tap(row: DeferredQuestion, index: int = 1, *, user: str = _OWNER, in_thread: bool = False) -> dict[str, Any]:
    option = OWNER_CARD.options[index - 1]
    message: dict[str, Any] = {"ts": "1800000000.000900" if in_thread else row.slack_ts}
    if in_thread:
        message["thread_ts"] = row.slack_ts
    return {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": row.slack_channel},
        "message": message,
        "actions": [
            {
                "type": "button",
                "action_id": f"owner-question-option-{index}",
                "value": str(index),
                "text": {"type": "plain_text", "text": option.label},
            }
        ],
    }


def _posted_card(**record_kwargs: Any) -> tuple[DeferredQuestion, MagicMock]:
    backend = _backend()
    DeferredQuestion.record("Can I ship the widgets?", **OWNER_DECISION, **record_kwargs)
    with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
        drain_unmirrored_deferred_questions(user_id=_OWNER, backend=backend)
    return DeferredQuestion.objects.get(), backend


class TestTheTapRoundTrip(TestCase):
    def test_only_registered_requester_can_tap_its_ticket_card(self) -> None:
        user = get_user_model().objects.create_superuser(username="alice", email="", password="unused")
        OperatorIdentity.objects.create(user=user, platform="github", handle="alice", external_id="123")
        OperatorIdentity.objects.create(user=user, platform="slack", handle="U-OP")
        ticket = Ticket.objects.create(requested_by="github:alice")
        row = DeferredQuestion.record(
            "Can I ship the widgets?",
            card=OWNER_CARD,
            task_session=Session.objects.create(ticket=ticket),
            slack_ts=_ROOT_TS,
            slack_channel=_CHANNEL,
        )
        backend = _backend()

        assert not answer_from_click(_tap(row, user=_OWNER), backend=backend)
        assert answer_from_click(_tap(row, user="U-OP"), backend=backend)
        row.refresh_from_db()
        assert row.answer_text == "Yes"
        assert backend.update_message.call_count == 1

    def test_a_tap_records_the_recommended_label_and_closes_the_root_card(self) -> None:
        row, backend = _posted_card()
        answered: list[DeferredQuestion] = []

        assert answer_from_click(_tap(row), backend=backend, on_answer=answered.append)

        row.refresh_from_db()
        assert [question.pk for question in answered] == [row.pk]
        assert (row.answer_text, row.resolved_via) == ("Yes", "slack")
        audit = DeferredQuestionAudit.objects.get(question=row, action="answered")
        assert audit.resolver_id == f"slack:{_OWNER}"
        backend.update_message.assert_called_once()
        update = backend.update_message.call_args.kwargs
        assert (update["channel"], update["ts"]) == (_CHANNEL, _ROOT_TS)
        assert "You chose: Yes - I go ahead." in update["text"]
        assert update["blocks"]
        assert all(block["type"] != "actions" for block in update["blocks"])
        assert update["blocks"][-1]["elements"][0]["text"] == f"ref: question {row.pk}"

    def test_every_update_carries_blocks(self) -> None:
        row, backend = _posted_card()

        answer_from_click(_tap(row), backend=backend)
        answer_from_click(_tap(row), backend=backend)

        assert backend.update_message.call_count == 2
        assert all(call.kwargs["blocks"] for call in backend.update_message.call_args_list)

    def test_a_second_tap_renders_already_answered_and_records_no_second_answer(self) -> None:
        row, backend = _posted_card()
        answer_from_click(_tap(row, 1), backend=backend)

        assert not answer_from_click(_tap(row, 2), backend=backend)

        row.refresh_from_db()
        assert row.answer_text == "Yes"
        assert "Already answered: Yes" in backend.update_message.call_args.kwargs["text"]

    def test_a_tap_by_another_user_writes_nothing_and_updates_nothing(self) -> None:
        row, backend = _posted_card()

        with self.assertLogs("teatree.loop.question_binding", level="WARNING") as logs:
            assert not answer_from_click(_tap(row, user="U0DEMOBOB"), backend=backend)

        row.refresh_from_db()
        assert row.is_pending
        assert "not the owner" in logs.output[0]
        backend.update_message.assert_not_called()

    def test_a_tap_naming_no_user_binds_nothing_while_the_owner_id_is_unknown(self) -> None:
        row, backend = _posted_card()
        backend.user_id = ""

        assert not answer_from_click(_tap(row, user=""), backend=backend)

        row.refresh_from_db()
        assert row.is_pending

    def test_a_tap_with_a_label_that_is_no_longer_the_option_is_stale_and_changes_nothing(self) -> None:
        row, backend = _posted_card()
        payload = _tap(row, 1)
        payload["actions"][0]["text"]["text"] = "Something else"

        assert not answer_from_click(payload, backend=backend)

        row.refresh_from_db()
        assert row.is_pending
        backend.update_message.assert_not_called()

    def test_a_tap_naming_an_option_outside_the_card_changes_nothing(self) -> None:
        row, backend = _posted_card()
        payload = _tap(row, 1)
        payload["actions"][0]["value"] = "9"

        assert not answer_from_click(payload, backend=backend)

        assert DeferredQuestion.objects.get(pk=row.pk).is_pending

    def test_a_tap_on_a_message_that_roots_no_question_changes_nothing(self) -> None:
        row, backend = _posted_card()
        payload = _tap(row)
        payload["message"] = {"ts": "1.0"}

        assert not answer_from_click(payload, backend=backend)

        assert DeferredQuestion.objects.get(pk=row.pk).is_pending

    def test_a_tap_that_is_no_owner_question_button_changes_nothing(self) -> None:
        row, backend = _posted_card()
        payload = _tap(row)
        payload["actions"][0]["action_id"] = "somebody-elses-button"

        assert not answer_from_click(payload, backend=backend)

        assert DeferredQuestion.objects.get(pk=row.pk).is_pending

    def test_a_tap_inside_the_thread_updates_the_root(self) -> None:
        row, backend = _posted_card()

        assert answer_from_click(_tap(row, in_thread=True), backend=backend)

        assert backend.update_message.call_args.kwargs["ts"] == _ROOT_TS

    def test_a_tap_on_a_nested_card_updates_the_card_itself(self) -> None:
        row, backend = _posted_card()
        nested_ts = "1800000000.000500"
        DeferredQuestion.objects.filter(pk=row.pk).update(slack_ts=nested_ts)
        row.refresh_from_db()
        payload = _tap(row)
        payload["message"]["thread_ts"] = _ROOT_TS

        assert answer_from_click(payload, backend=backend)

        row.refresh_from_db()
        assert row.answer_text == "Yes"
        assert backend.update_message.call_args.kwargs["ts"] == nested_ts

    def test_a_nested_card_tap_selects_its_own_timestamp_before_the_root(self) -> None:
        root, backend = _posted_card()
        nested = DeferredQuestion.record(
            "Can I ship the other widgets?",
            card=OWNER_CARD,
            slack_channel=_CHANNEL,
            slack_ts="1800000000.000500",
        )
        payload = _tap(nested)
        payload["message"]["thread_ts"] = root.slack_ts

        assert answer_from_click(payload, backend=backend)

        root.refresh_from_db()
        nested.refresh_from_db()
        assert root.is_pending
        assert nested.answer_text == "Yes"
        assert backend.update_message.call_args.kwargs["ts"] == nested.slack_ts

    def test_a_tap_on_a_dismissed_question_says_it_no_longer_needs_an_answer(self) -> None:
        row, backend = _posted_card()
        row.mark_stale("the widgets shipped without asking")

        assert not answer_from_click(_tap(row), backend=backend)

        assert "This question no longer needs an answer." in backend.update_message.call_args.kwargs["text"]
        assert DeferredQuestion.objects.get(pk=row.pk).answer_text == ""

    def test_a_database_error_leaves_the_buttons_in_place(self) -> None:
        row, backend = _posted_card()

        with (
            patch.object(DeferredQuestion, "apply_answer", side_effect=OperationalError("database is locked")),
            contextlib.suppress(OperationalError),
        ):
            answer_from_click(_tap(row), backend=backend)

        backend.update_message.assert_not_called()
        assert DeferredQuestion.objects.get(pk=row.pk).is_pending

    def test_a_failed_edit_never_undoes_the_recorded_answer(self) -> None:
        row, backend = _posted_card()
        backend.update_message.side_effect = RuntimeError("slack is down")

        assert answer_from_click(_tap(row), backend=backend)

        assert DeferredQuestion.objects.get(pk=row.pk).answer_text == "Yes"

    def test_an_answered_question_is_not_re_asked_two_hours_later(self) -> None:
        row, backend = _posted_card()
        answer_from_click(_tap(row), backend=backend)
        backend.post_message.reset_mock()

        later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)
        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            bumped, candidates = reask_escalated_questions(user_id=_OWNER, backend=backend, now=later)

        assert (bumped, candidates) == (0, 0)
        backend.post_message.assert_not_called()


class TestACardWhoseProducerKeepsItsOwnKey(TestCase):
    def test_a_tap_records_the_option_although_the_hash_column_is_the_producers_dedupe_key(self) -> None:
        row, backend = _posted_card(options_hash="producer_key:7:1")

        assert answer_from_click(_tap(row, 2), backend=backend)

        row.refresh_from_db()
        assert (row.answer_text, row.resolved_via, row.options_hash) == ("No", "slack", "producer_key:7:1")
        assert "You chose: No - I leave it as it is." in backend.update_message.call_args.kwargs["text"]

    def test_a_stale_label_still_changes_nothing_on_such_a_card(self) -> None:
        row, backend = _posted_card(options_hash="producer_key:7:1")
        payload = _tap(row, 1)
        payload["actions"][0]["text"]["text"] = "Something else"

        assert not answer_from_click(payload, backend=backend)

        assert DeferredQuestion.objects.get(pk=row.pk).is_pending
        backend.update_message.assert_not_called()


class TestTheTapResumesTheParkedTask(TestCase):
    def test_a_tap_resumes_the_task_waiting_on_the_card(self) -> None:
        ticket = planned_ticket()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="shipping")
        row, backend = _posted_card(parked_task=task, task_session=task.session)

        assert answer_from_click(_tap(row), backend=backend)

        assert task.child_tasks.count() == 1
