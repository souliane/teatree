"""The coordinator proposes, code disposes: each action runs once from the closed table or runs nothing (#5685)."""

from dataclasses import dataclass, field
from unittest import mock

from django.test import TestCase

from teatree.agents.reactive_envelope_recorders import record_reactive_envelopes
from teatree.core.answering.coordinator_actions import AUDIT_KEY, PeerReceipt, run_coordinator_actions
from teatree.core.modelkit.owner_decision import OWNER_KEPT_DECISIONS, OwnerDecision
from teatree.core.models import DeferredQuestion, Session, Task, ThreadBinding, Ticket
from teatree.types import RawAPIDict
from teatree.utils.url_slug import conversation_anchor, slack_conversation_anchor
from tests._owner_channel import owner_card

_CHANNEL = "D-owner"
_WORK_URL = "https://github.com/souliane/teatree/issues/4084"


@dataclass
class RecordingBackend:
    replies: list[tuple[str, str, str]] = field(default_factory=list)

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        self.replies.append((channel, ts, text))
        return {"ok": True}


def _bind(kind: str = "bind_thread", **arguments: str) -> dict[str, object]:
    return {"answer": {"text": "Done."}, "actions": [{"kind": kind, **arguments}]}


class TestTheCoordinatorsActionsRunFromTheClosedTable(TestCase):
    def setUp(self) -> None:
        self.backend = RecordingBackend()
        patcher = mock.patch("teatree.core.backend_factory.messaging_from_overlay", return_value=self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = Ticket.objects.create(overlay="t3-teatree", issue_url=_WORK_URL, short_description="Fix rounding")
        self.task = self._answering_task(question=f"please move this thread to {_WORK_URL}")
        self.conversation_id = self.task.ticket_id

    def _answering_task(self, *, question: str) -> Task:
        ticket = Ticket.objects.create(
            issue_url=slack_conversation_anchor(channel=_CHANNEL, slack_ts="1.0"),
            overlay="t3-teatree",
            short_description="move the thread",
            extra={"slack_answer": {"channel": _CHANNEL, "slack_ts": "1.0", "question": question}},
        )
        ThreadBinding.bind(ticket=ticket, channel=_CHANNEL, thread_ts="1.0")
        session = Session.objects.create(ticket=ticket, overlay="t3-teatree", agent_id="answering")
        return Task.objects.create(ticket=ticket, session=session, phase="answering")

    def _record(self, result: dict[str, object], *, phase: str = "answering") -> None:
        record_reactive_envelopes(self.task, result, phase=phase)

    def _bound_to(self) -> int:
        return ThreadBinding.objects.get(channel=_CHANNEL, thread_ts="1.0").ticket_id

    def _audit(self) -> dict[str, dict[str, object]]:
        self.task.ticket.refresh_from_db()
        return self.task.ticket.extra.get(AUDIT_KEY, {})

    def test_a_ticket_the_owner_named_takes_the_thread_and_the_receipt_says_so(self) -> None:
        self._record(_bind(ticket=_WORK_URL))

        assert self._bound_to() == self.work.pk
        assert f"Bound this thread to <{_WORK_URL}|#4084>." in self.backend.replies[0][2]
        assert [(row["kind"], row["executed"]) for row in self._audit().values()] == [("bind_thread", True)]

    def test_the_same_proposal_runs_once_however_often_it_is_recorded(self) -> None:
        self._record(_bind(ticket=_WORK_URL))
        self._record(_bind(ticket=_WORK_URL))

        assert [row["executed"] for row in self._audit().values()] == [True]
        assert "Bound this thread" not in self.backend.replies[1][2]

    def test_queue_task_plans_the_conversation_with_a_code_owned_reason(self) -> None:
        conversation = self.task.ticket

        self._record(_bind(kind="queue_task", ticket=conversation.issue_url))

        queued = Task.objects.get(ticket=conversation, phase="planning")
        assert queued.status == Task.Status.PENDING
        assert queued.execution_reason == "Plan this ticket from its recorded context."
        assert "please move" not in queued.execution_reason
        assert f"TODO-{queued.pk}" in self.backend.replies[0][2]
        assert [(row["kind"], row["executed"]) for row in self._audit().values()] == [("queue_task", True)]

    def test_queue_task_accepts_only_a_recorded_or_owner_named_work_ticket(self) -> None:
        other = Ticket.objects.create(
            overlay="t3-teatree", issue_url="https://github.com/souliane/teatree/issues/9", short_description="Other"
        )
        self._record(_bind(kind="queue_task", ticket=other.issue_url))
        assert not Task.objects.filter(ticket=other, phase="planning").exists()

        self.task.ticket.merge_extra(merge_into_dicts={"slack_answer": {"work_issue_url": _WORK_URL}})
        self._record(_bind(kind="queue_task", ticket=_WORK_URL))

        queued = Task.objects.get(ticket=self.work, phase="planning")
        assert queued.execution_reason == "Plan this ticket from its recorded context."
        assert [row["executed"] for row in self._audit().values()] == [False, True]

    def test_queue_task_reuses_an_active_planning_task(self) -> None:
        session = Session.objects.create(ticket=self.work, overlay="t3-teatree", agent_id="planning")
        active = Task.objects.create(ticket=self.work, session=session, phase="planning", status=Task.Status.CLAIMED)

        self._record(_bind(kind="queue_task", ticket=_WORK_URL))

        assert list(Task.objects.filter(ticket=self.work, phase="planning")) == [active]
        assert f"TODO-{active.pk}" in self.backend.replies[0][2]

    def test_queue_task_admits_a_mailbox_conversation_without_slack_context(self) -> None:
        mailbox = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url=conversation_anchor(kind="mailbox", key="ticket/42/agent/a"),
            short_description="Plan this peer request",
        )
        session = Session.objects.create(ticket=mailbox, overlay="t3-teatree", agent_id="answering")
        self.task = Task.objects.create(ticket=mailbox, session=session, phase="answering")

        self._record(_bind(kind="queue_task", ticket=mailbox.issue_url))

        assert Task.objects.filter(ticket=mailbox, phase="planning").count() == 1
        assert [row["executed"] for row in self._audit().values()] == [True]

    def test_queue_task_refuses_missing_ticket_and_closed_work(self) -> None:
        self._record({"answer": {"text": "Done."}, "actions": [{"kind": "queue_task"}]})
        Ticket.objects.filter(pk=self.work.pk).update(state=Ticket.State.DELIVERED)
        self._record(_bind(kind="queue_task", ticket=_WORK_URL))

        assert not Task.objects.filter(ticket=self.work, phase="planning").exists()
        assert [row["detail"] for row in self._audit().values()] == [
            "queue_task needs a ticket URL",
            "no open tracked ticket matches it",
        ]

    def test_queue_task_refuses_a_ticket_past_planning(self) -> None:
        Ticket.objects.filter(pk=self.work.pk).update(state=Ticket.State.TESTED)

        self._record(_bind(kind="queue_task", ticket=_WORK_URL))

        assert not Task.objects.filter(ticket=self.work, phase="planning").exists()
        assert "planning is not available from tested" in self.backend.replies[0][2]

    def test_queue_task_refuses_a_ticket_whose_role_no_sub_agent_plans(self) -> None:
        Ticket.objects.filter(pk=self.work.pk).update(role=Ticket.Role.REVIEWER)

        self._record(_bind(kind="queue_task", ticket=_WORK_URL))

        reply = self.backend.replies[0][2]
        assert not Task.objects.filter(ticket=self.work, phase="planning").exists()
        assert "Could not run queue_task: no sub-agent plans a reviewer ticket." in reply
        assert "Planning is queued" not in reply

    def test_an_action_refusal_is_stated_to_the_owner(self) -> None:
        self._record(_bind(ticket=_WORK_URL, channel="C-eng"))

        assert "Could not run bind_thread: bind_thread takes no channel" in self.backend.replies[0][2]

    def test_a_later_action_failure_keeps_the_earlier_receipt(self) -> None:
        requests = [
            {"kind": "bind_thread", "ticket": _WORK_URL},
            {"kind": "queue_task", "ticket": self.task.ticket.issue_url},
        ]

        with mock.patch(
            "teatree.core.answering.coordinator_actions.enqueue_phase_task", side_effect=RuntimeError("db down")
        ):
            self._record({"answer": {"text": "Done."}, "actions": requests})

        assert self._bound_to() == self.work.pk
        reply = self.backend.replies[0][2]
        assert f"Bound this thread to <{_WORK_URL}|#4084>." in reply
        assert "Could not confirm queue_task" in reply
        assert not Task.objects.filter(ticket_id=self.conversation_id, phase="planning").exists()

    def test_queue_task_refuses_a_task_outside_a_conversation(self) -> None:
        session = Session.objects.create(ticket=self.work, overlay="t3-teatree", agent_id="answering")
        self.task = Task.objects.create(ticket=self.work, session=session, phase="answering")

        self._record(_bind(kind="queue_task", ticket=_WORK_URL))

        assert not Task.objects.filter(ticket=self.work, phase="planning").exists()
        assert [row["detail"] for row in self._audit().values()] == ["the task's ticket is not a conversation"]

    def test_a_ticket_the_owner_did_not_name_runs_nothing_and_the_receipt_claims_nothing(self) -> None:
        other = Ticket.objects.create(
            overlay="t3-teatree", issue_url="https://github.com/souliane/teatree/issues/9", short_description="Other"
        )
        self._record(_bind(ticket=other.issue_url))

        assert self._bound_to() == self.conversation_id
        assert "Bound" not in self.backend.replies[0][2]
        assert [row["executed"] for row in self._audit().values()] == [False]

    def test_an_unknown_kind_runs_nothing(self) -> None:
        self._record(_bind(kind="post_to_colleague", channel="C-eng", text="hello"))

        assert self._bound_to() == self.conversation_id
        assert [row["detail"] for row in self._audit().values()] == ["unknown action kind 'post_to_colleague'"]

    def test_an_argument_the_kind_does_not_declare_runs_nothing(self) -> None:
        self._record(_bind(ticket=_WORK_URL, channel="C-eng"))

        assert self._bound_to() == self.conversation_id
        assert [row["detail"] for row in self._audit().values()] == ["bind_thread takes no channel"]

    def test_a_ticket_no_longer_open_runs_nothing(self) -> None:
        Ticket.objects.filter(pk=self.work.pk).update(state=Ticket.State.DELIVERED)

        self._record(_bind(ticket=_WORK_URL))

        assert self._bound_to() == self.conversation_id

    def test_a_thread_that_is_not_bound_runs_nothing(self) -> None:
        ThreadBinding.objects.all().delete()

        self._record(_bind(ticket=_WORK_URL))

        assert [row["detail"] for row in self._audit().values()] == ["this thread is not bound"]

    def test_a_task_on_a_tracked_ticket_runs_nothing(self) -> None:
        tracked = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url="https://github.com/souliane/teatree/issues/7",
            short_description="Tracked",
            extra={"slack_answer": {"question": f"move this to {_WORK_URL}"}},
        )
        ThreadBinding.bind(ticket=tracked, channel=_CHANNEL, thread_ts="2.0")
        session = Session.objects.create(ticket=tracked, overlay="t3-teatree", agent_id="answering")
        self.task = Task.objects.create(ticket=tracked, session=session, phase="answering")

        self._record(_bind(ticket=_WORK_URL))

        assert ThreadBinding.objects.get(channel=_CHANNEL, thread_ts="2.0").ticket_id == tracked.pk

    def test_bind_thread_does_not_rebind_a_non_slack_conversation(self) -> None:
        mailbox = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url=conversation_anchor(kind="mailbox", key="ticket/42/agent/a"),
            extra={"slack_answer": {"question": f"move this to {_WORK_URL}"}},
        )
        ThreadBinding.bind(ticket=mailbox, channel=_CHANNEL, thread_ts="2.0")
        session = Session.objects.create(ticket=mailbox, overlay="t3-teatree", agent_id="answering")
        self.task = Task.objects.create(ticket=mailbox, session=session, phase="answering")

        self._record(_bind(ticket=_WORK_URL))

        assert ThreadBinding.objects.get(channel=_CHANNEL, thread_ts="2.0").ticket_id == mailbox.pk
        assert [row["detail"] for row in self._audit().values()] == ["the task's ticket is not a Slack conversation"]

    def test_actions_on_another_phase_are_ignored(self) -> None:
        self._record(_bind(ticket=_WORK_URL), phase="planning")

        assert self._bound_to() == self.conversation_id
        assert self._audit() == {}

    def test_bind_thread_refuses_a_tracked_ticket_whose_remote_is_gone(self) -> None:
        Ticket.objects.filter(pk=self.work.pk).update(remote_missing=True)

        self._record(_bind(ticket=_WORK_URL))

        assert self._bound_to() == self.conversation_id
        assert [row["detail"] for row in self._audit().values()] == ["no open tracked ticket matches it"]

    def test_an_actions_value_that_is_not_a_list_runs_and_states_nothing(self) -> None:
        self._record({"answer": {"text": "Done."}, "actions": {"kind": "bind_thread", "ticket": _WORK_URL}})

        assert self._bound_to() == self.conversation_id
        assert self._audit() == {}
        assert "Could not" not in self.backend.replies[0][2]

    def test_an_entry_that_is_not_an_object_is_refused_and_the_next_one_still_runs(self) -> None:
        self._record(
            {"answer": {"text": "Done."}, "actions": ["bind_thread", {"kind": "bind_thread", "ticket": _WORK_URL}]}
        )

        assert self._bound_to() == self.work.pk
        assert "Could not run action: an action must be an object." in self.backend.replies[0][2]

    def test_an_unexpected_failure_stops_the_entries_after_it(self) -> None:
        requests = [
            {"kind": "queue_task", "ticket": self.task.ticket.issue_url},
            {"kind": "bind_thread", "ticket": _WORK_URL},
        ]

        with mock.patch(
            "teatree.core.answering.coordinator_actions.enqueue_phase_task", side_effect=RuntimeError("db down")
        ):
            self._record({"answer": {"text": "Done."}, "actions": requests})

        reply = self.backend.replies[0][2]
        assert self._bound_to() == self.conversation_id
        assert "Could not confirm queue_task" in reply
        assert "Bound this thread" not in reply

    def test_a_later_answering_task_runs_the_same_proposal_again(self) -> None:
        conversation = self.task.ticket
        self._record(_bind(kind="queue_task", ticket=conversation.issue_url))
        self.task = Task.objects.create(ticket=conversation, session=self.task.session, phase="answering")

        self._record(_bind(kind="queue_task", ticket=conversation.issue_url))

        planning = Task.objects.get(ticket=conversation, phase="planning")
        assert f"TODO-{planning.pk}" in self.backend.replies[1][2]
        assert len(self._audit()) == 2

    def test_a_failure_in_the_action_machinery_still_delivers_the_reply(self) -> None:
        with (
            mock.patch(
                "teatree.agents.reactive_envelope_recorders.run_coordinator_actions", side_effect=RuntimeError("down")
            ),
            self.assertLogs("teatree.agents.reactive_envelope_recorders", "ERROR"),
        ):
            self._record(_bind(ticket=_WORK_URL))

        assert "Done." in self.backend.replies[0][2]
        assert self._bound_to() == self.conversation_id

    def test_a_malformed_list_runs_nothing(self) -> None:
        self._record({"answer": {"text": "Done."}, "actions": ["bind_thread", 7, {"ticket": _WORK_URL}]})

        assert self._bound_to() == self.conversation_id


_QUESTION = "Should we adopt the new dependency?"
_EARLIER_ANSWER = "Use the new dependency."


class TestSettleCardAnswersFromARecordedOwnerDecision(TestCase):
    def setUp(self) -> None:
        self.backend = RecordingBackend()
        patcher = mock.patch("teatree.core.backend_factory.messaging_from_overlay", return_value=self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)
        ticket = Ticket.objects.create(
            issue_url=slack_conversation_anchor(channel=_CHANNEL, slack_ts="1.0"),
            overlay="t3-teatree",
            short_description="settle a card",
            extra={"slack_answer": {"channel": _CHANNEL, "slack_ts": "1.0", "question": "settle the card"}},
        )
        ThreadBinding.bind(ticket=ticket, channel=_CHANNEL, thread_ts="1.0")
        session = Session.objects.create(ticket=ticket, overlay="t3-teatree", agent_id="answering")
        self.task = Task.objects.create(ticket=ticket, session=session, phase="answering")

    def _owner_says(self, text: str) -> None:
        self.task.ticket.merge_extra(merge_into_dicts={"slack_answer": {"question": text}})

    def _answered_before(self, question: str = _QUESTION, decision: OwnerDecision = OwnerDecision.ARCHITECTURE) -> None:
        basis = DeferredQuestion.record(question, card=owner_card(decision))
        assert basis.apply_answer(_EARLIER_ANSWER, resolved_via=DeferredQuestion.ResolvedVia.SLACK)

    def _pending(self, question: str = _QUESTION, decision: OwnerDecision = OwnerDecision.ARCHITECTURE):
        return DeferredQuestion.record(question, card=owner_card(decision))

    def _settle(self, card: object) -> list[str]:
        return run_coordinator_actions(self.task, [{"kind": "settle_card", "card": card}])

    def _audit(self) -> list[tuple[bool, str]]:
        self.task.ticket.refresh_from_db()
        return [(row["executed"], row["detail"]) for row in self.task.ticket.extra[AUDIT_KEY].values()]

    def test_a_card_the_owner_named_is_answered_with_the_owners_earlier_answer(self) -> None:
        self._answered_before()
        pending = self._pending()
        self._owner_says(f"please settle card {pending.pk} like last time")

        receipts = self._settle(str(pending.pk))

        pending.refresh_from_db()
        assert (pending.answer_text, pending.resolved_via) == (_EARLIER_ANSWER, DeferredQuestion.ResolvedVia.AGENT)
        assert pending.audits.get(action="coordinated").resolver_id == "coordinator"
        assert receipts == [f"Settled card {pending.pk} with your earlier answer: {_EARLIER_ANSWER}"]
        assert [executed for executed, _ in self._audit()] == [True]

    def test_the_receipt_reaches_the_owner_through_the_recorded_envelope(self) -> None:
        self._answered_before()
        pending = self._pending()
        self._owner_says(f"settle #{pending.pk}")

        record_reactive_envelopes(
            self.task,
            {"answer": {"text": "Done."}, "actions": [{"kind": "settle_card", "card": str(pending.pk)}]},
            phase="answering",
        )

        assert f"Settled card {pending.pk} with your earlier answer" in self.backend.replies[0][2]

    def test_every_way_of_naming_the_card_counts(self) -> None:
        for wording in ("card {id}", "Card #{id}", "Question {id}", "cards {id}", "#{id}", "settle #{id}, thanks"):
            with self.subTest(wording=wording):
                self._answered_before()
                pending = self._pending()
                self._owner_says(wording.format(id=pending.pk))

                receipts = self._settle(str(pending.pk))

                assert receipts == [f"Settled card {pending.pk} with your earlier answer: {_EARLIER_ANSWER}"]

    def test_the_same_proposal_settles_once(self) -> None:
        self._answered_before()
        pending = self._pending()
        self._owner_says(f"settle card {pending.pk}")

        self._settle(str(pending.pk))

        assert self._settle(str(pending.pk)) == []
        assert pending.audits.filter(action="coordinated").count() == 1

    def test_a_card_the_owner_did_not_name_is_left_pending(self) -> None:
        self._answered_before()
        pending = self._pending()

        receipts = self._settle(str(pending.pk))

        pending.refresh_from_db()
        assert pending.is_pending
        assert receipts == ["Could not run settle_card: the owner's message does not name that card."]

    def test_a_number_that_is_not_a_card_mention_does_not_name_the_card(self) -> None:
        for wording in ("see issue {id}", "https://github.com/o/r/issues/{id}", "card {id}0", "discard {id}", "{id}"):
            with self.subTest(wording=wording):
                self._answered_before()
                pending = self._pending()
                self._owner_says(wording.format(id=pending.pk))

                self._settle(str(pending.pk))

                pending.refresh_from_db()
                assert pending.is_pending

    def test_a_card_deciding_one_of_the_owners_limits_is_refused_and_left_pending(self) -> None:
        for decision in sorted(OWNER_KEPT_DECISIONS):
            with self.subTest(decision=decision):
                self._answered_before("May I do the thing?", decision)
                pending = self._pending("May I do the thing?", decision)
                self._owner_says(f"settle card {pending.pk}")

                receipts = self._settle(str(pending.pk))

                pending.refresh_from_db()
                assert pending.is_pending
                assert receipts == [f"Could not run settle_card: settle_card would cross the owner's {decision} limit."]

    def test_a_card_with_no_recorded_decision_kind_is_refused(self) -> None:
        self._answered_before()
        pending = self._pending()
        DeferredQuestion.objects.filter(pk=pending.pk).update(evidence={})
        self._owner_says(f"settle card {pending.pk}")

        receipts = self._settle(str(pending.pk))

        assert receipts == ["Could not run settle_card: settle_card needs a card with a recorded decision kind."]

    def test_a_card_with_no_recorded_owner_answer_is_left_pending(self) -> None:
        pending = self._pending()
        self._owner_says(f"settle card {pending.pk}")

        receipts = self._settle(str(pending.pk))

        pending.refresh_from_db()
        assert pending.is_pending
        assert receipts == ["Could not run settle_card: no recorded owner answer matches that card."]

    def test_an_answer_an_agent_gave_is_not_a_recorded_owner_answer(self) -> None:
        basis = DeferredQuestion.record(_QUESTION, card=owner_card())
        assert basis.apply_answer(_EARLIER_ANSWER, resolved_via=DeferredQuestion.ResolvedVia.AGENT)
        pending = self._pending()
        self._owner_says(f"settle card {pending.pk}")

        self._settle(str(pending.pk))

        pending.refresh_from_db()
        assert pending.is_pending

    def test_only_a_pending_owner_card_can_be_settled(self) -> None:
        for state in ("answered", "dismissed", "internal"):
            with self.subTest(state=state):
                self._answered_before()
                pending = self._pending()
                if state == "answered":
                    pending.apply_answer("No.", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
                elif state == "dismissed":
                    pending.mark_stale("moot", resolver_id="test")
                else:
                    DeferredQuestion.objects.filter(pk=pending.pk).update(audience=DeferredQuestion.Audience.INTERNAL)
                self._owner_says(f"settle card {pending.pk}")

                receipts = self._settle(str(pending.pk))

                assert receipts == ["Could not run settle_card: no pending owner card has that number."]
                assert not pending.audits.filter(action="coordinated").exists()

    def test_a_card_that_is_not_a_number_runs_nothing(self) -> None:
        for card in (None, "", "abc", "12 or 13", 12.5, ["12"]):
            with self.subTest(card=card):
                assert self._settle(card) == ["Could not run settle_card: settle_card needs a card number."]

    def test_a_card_settled_by_someone_else_in_the_meantime_is_reported_not_claimed(self) -> None:
        self._answered_before()
        pending = self._pending()
        self._owner_says(f"settle card {pending.pk}")

        with mock.patch("teatree.core.answering.coordinator_actions.coordinate_answer", return_value=None):
            receipts = self._settle(str(pending.pk))

        assert receipts == ["Could not run settle_card: that card cannot be settled from the owner's earlier answer."]


class _Messenger:
    def __init__(self, receipt: PeerReceipt | None = None) -> None:
        self.receipt = receipt or PeerReceipt(accepted=True, detail="")
        self.calls: list[tuple[int, str, str]] = []

    def __call__(self, ticket: Ticket, text: str, key: str) -> PeerReceipt:
        self.calls.append((ticket.pk, text, key))
        return self.receipt


class TestMessagePeerReachesAnAgentOnABoundTicketThroughTheMailbox(TestCase):
    def setUp(self) -> None:
        self.work = Ticket.objects.create(overlay="t3-teatree", issue_url=_WORK_URL, short_description="Fix rounding")
        ticket = Ticket.objects.create(
            issue_url=slack_conversation_anchor(channel=_CHANNEL, slack_ts="1.0"),
            overlay="t3-teatree",
            short_description="talk to the agent",
            extra={"slack_answer": {"channel": _CHANNEL, "slack_ts": "1.0", "question": f"tell {_WORK_URL} to use B"}},
        )
        ThreadBinding.bind(ticket=ticket, channel=_CHANNEL, thread_ts="1.0")
        session = Session.objects.create(ticket=ticket, overlay="t3-teatree", agent_id="answering")
        self.task = Task.objects.create(ticket=ticket, session=session, phase="answering")
        self.messenger = _Messenger()

    def _send(self, *, messenger: _Messenger | None = None, **arguments: object) -> list[str]:
        request = {"kind": "message_peer", "ticket": _WORK_URL, "text": "Use approach B.", **arguments}
        return run_coordinator_actions(
            self.task, [request], messenger=self.messenger if messenger is None else messenger
        )

    def _audit(self) -> dict[str, dict[str, object]]:
        self.task.ticket.refresh_from_db()
        return self.task.ticket.extra[AUDIT_KEY]

    def test_the_text_goes_to_the_agent_on_the_named_ticket_under_the_audit_key(self) -> None:
        receipts = self._send()

        (audit_key,) = self._audit()
        assert self.messenger.calls == [(self.work.pk, "Use approach B.", audit_key)]
        assert receipts == [f"Sent your message to the agent working on <{_WORK_URL}|#4084>."]
        assert [row["executed"] for row in self._audit().values()] == [True]

    def test_the_same_proposal_sends_once(self) -> None:
        self._send()

        assert self._send() == []
        assert len(self.messenger.calls) == 1

    def test_a_ticket_recorded_as_the_conversations_work_item_is_reachable(self) -> None:
        self.task.ticket.merge_extra(
            merge_into_dicts={"slack_answer": {"question": "tell it", "work_issue_url": _WORK_URL}}
        )

        assert self._send() == [f"Sent your message to the agent working on <{_WORK_URL}|#4084>."]

    def test_a_ticket_the_owner_did_not_name_is_not_messaged(self) -> None:
        other = Ticket.objects.create(
            overlay="t3-teatree", issue_url="https://github.com/souliane/teatree/issues/9", short_description="Other"
        )

        receipts = self._send(ticket=other.issue_url)

        assert self.messenger.calls == []
        assert receipts == ["Could not run message_peer: the conversation does not identify that work ticket."]

    def test_the_conversation_itself_is_not_a_peer(self) -> None:
        receipts = self._send(ticket=self.task.ticket.issue_url)

        assert self.messenger.calls == []
        assert receipts == ["Could not run message_peer: the conversation does not identify that work ticket."]

    def test_a_ticket_no_longer_open_is_not_messaged(self) -> None:
        Ticket.objects.filter(pk=self.work.pk).update(state=Ticket.State.DELIVERED)

        receipts = self._send()

        assert self.messenger.calls == []
        assert receipts == ["Could not run message_peer: no open tracked ticket matches it."]

    def test_a_task_outside_a_conversation_sends_nothing(self) -> None:
        session = Session.objects.create(ticket=self.work, overlay="t3-teatree", agent_id="answering")
        self.task = Task.objects.create(ticket=self.work, session=session, phase="answering")

        receipts = self._send()

        assert self.messenger.calls == []
        assert receipts == ["Could not run message_peer: the task's ticket is not a conversation."]

    def test_a_missing_ticket_sends_nothing(self) -> None:
        for ticket in ("", None, 4084):
            with self.subTest(ticket=ticket):
                assert self._send(ticket=ticket) == ["Could not run message_peer: message_peer needs a ticket URL."]
        assert self.messenger.calls == []

    def test_a_missing_message_sends_nothing(self) -> None:
        for text in ("", "   ", None, 7, ["hi"]):
            with self.subTest(text=text):
                assert self._send(text=text) == ["Could not run message_peer: message_peer needs a message."]
        assert self.messenger.calls == []

    def test_a_message_over_the_length_bound_sends_nothing(self) -> None:
        assert self._send(text="x" * 1001) == ["Could not run message_peer: message_peer text is over 1000 characters."]
        assert self.messenger.calls == []

    def test_a_message_at_the_length_bound_is_sent(self) -> None:
        self._send(text="x" * 1000)

        assert len(self.messenger.calls) == 1

    def test_with_no_mailbox_connected_nothing_is_sent_and_the_refusal_says_so(self) -> None:
        receipts = run_coordinator_actions(
            self.task, [{"kind": "message_peer", "ticket": _WORK_URL, "text": "Use approach B."}]
        )

        assert receipts == ["Could not run message_peer: no live mailbox is connected in this process."]
        assert [row["executed"] for row in self._audit().values()] == [False]

    def test_a_peer_that_did_not_accept_is_reported_with_its_reason_not_claimed(self) -> None:
        refusing = _Messenger(PeerReceipt(accepted=False, detail="no live agent works on that ticket"))

        receipts = self._send(messenger=refusing)

        assert receipts == ["Could not run message_peer: no live agent works on that ticket."]
        assert [row["executed"] for row in self._audit().values()] == [False]

    def test_a_mailbox_failure_stops_the_entries_after_it(self) -> None:
        failing = mock.Mock(side_effect=OSError("socket closed"))
        requests = [
            {"kind": "message_peer", "ticket": _WORK_URL, "text": "Use approach B."},
            {"kind": "message_peer", "ticket": _WORK_URL, "text": "And approach C."},
        ]

        receipts = run_coordinator_actions(self.task, requests, messenger=failing)

        assert receipts == ["Could not confirm message_peer; earlier confirmed actions are listed above."]

    def test_an_undeclared_argument_sends_nothing(self) -> None:
        receipts = self._send(channel="C-eng")

        assert self.messenger.calls == []
        assert receipts == ["Could not run message_peer: message_peer takes no channel."]
