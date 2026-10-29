"""The coordinator proposes, code disposes: each action runs once from the closed table or runs nothing (#5685)."""

from dataclasses import dataclass, field
from unittest import mock

from django.test import TestCase

from teatree.agents.reactive_envelope_recorders import record_reactive_envelopes
from teatree.core.answering.coordinator_actions import AUDIT_KEY
from teatree.core.models import Session, Task, ThreadBinding, Ticket
from teatree.types import RawAPIDict
from teatree.utils.url_slug import conversation_anchor, slack_conversation_anchor

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

    def test_a_malformed_list_runs_nothing(self) -> None:
        self._record({"answer": {"text": "Done."}, "actions": ["bind_thread", 7, {"ticket": _WORK_URL}]})

        assert self._bound_to() == self.conversation_id
