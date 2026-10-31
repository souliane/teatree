# test-path: cross-cutting — `questions record` settles what code can, and hands the rest to the coordinator.
"""`questions record` raises no card for what the code can settle; a need no owner decides goes to the coordinator."""

import dataclasses
import datetime as dt
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone

from teatree.agents.live_control import PeerIdentity
from teatree.agents.live_delivery import SendReceipt
from teatree.agents.live_mailbox_client import MailboxClient, MailboxError
from teatree.core.answering.escalation import ESCALATION_DEADLINE, MARKER_PREFIX
from teatree.core.answering.record_settling import LIMIT_CHOICES
from teatree.core.modelkit.owner_decision import OWNER_KEPT_DECISIONS, OwnerDecision
from teatree.core.models import Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.question_text import question_fingerprint
from tests.factories import SessionFactory, TaskFactory, TicketFactory

_QUESTION = "Should we adopt the new dependency?"
_ANSWER = "Use the new dependency."
_FIRST_TICKET_MARKER = f"needs-input:101:{question_fingerprint(_QUESTION)}"
_SECOND_TICKET_MARKER = f"needs-input:202:{question_fingerprint(_QUESTION)}"
_ARCHITECTURE_OPTIONS = [
    {"label": "Adopt it", "description": "I add the dependency.", "recommended": True},
    {"label": "Build it", "description": "I write the code myself."},
]
_LIMIT_QUESTION = "May I go ahead with this step?"
_COORDINATOR = "coordinator-address"
_ASKER = "asker-address"


def _record(question: str, *flags: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        call_command("questions", "record", question, *flags, stdout=out, stderr=err)
    except SystemExit as exit_:
        code = int(exit_.code or 0)
    return code, out.getvalue(), err.getvalue()


def _card_flags(
    decision: str, options: list[dict[str, object]], *, blocker: str = "Only the owner can choose."
) -> tuple[str, ...]:
    flags = (
        "--decision",
        decision,
        "--checked",
        "No standing answer covers it.",
        "--why",
        "The answer changes what the factory does next.",
        "--options",
        json.dumps(options),
    )
    return (*flags, "--blocker", blocker) if blocker else flags


def _owner_cards() -> int:
    return DeferredQuestion.objects.filter(audience=DeferredQuestion.Audience.OWNER_QUESTION).count()


def _owner_answered_card(marker: str, *, question: str = _QUESTION, decision: str = "architecture") -> DeferredQuestion:
    _record(question, *_card_flags(decision, _ARCHITECTURE_OPTIONS), "--dedupe-marker", marker)
    row = DeferredQuestion.objects.get(dedupe_marker=marker)
    assert row.apply_answer(_ANSWER, resolved_via=DeferredQuestion.ResolvedVia.SLACK)
    return row


class TestAnOwnerAnswerFromTheLastDayIsReused(TestCase):
    def test_the_same_question_from_another_ticket_gets_the_answer_and_records_nothing(self) -> None:
        basis = _owner_answered_card(_FIRST_TICKET_MARKER)

        code, out, _err = _record(
            _QUESTION, *_card_flags("architecture", _ARCHITECTURE_OPTIONS), "--dedupe-marker", _SECOND_TICKET_MARKER
        )

        assert code == 0
        assert _ANSWER in out
        assert f"#{basis.pk}" in out
        assert list(DeferredQuestion.objects.values_list("pk", flat=True)) == [basis.pk]

    def test_an_answer_just_inside_the_window_is_reused(self) -> None:
        basis = _owner_answered_card(_FIRST_TICKET_MARKER)
        inside = timezone.now() - dt.timedelta(hours=23)
        DeferredQuestion.objects.filter(pk=basis.pk).update(answered_at=inside)

        code, out, _err = _record(
            _QUESTION, *_card_flags("architecture", _ARCHITECTURE_OPTIONS), "--dedupe-marker", _SECOND_TICKET_MARKER
        )

        assert (code, _ANSWER in out, _owner_cards()) == (0, True, 1)

    def test_an_answer_just_outside_the_window_is_not_reused_and_one_card_is_recorded(self) -> None:
        basis = _owner_answered_card(_FIRST_TICKET_MARKER)
        outside = timezone.now() - dt.timedelta(hours=25)
        DeferredQuestion.objects.filter(pk=basis.pk).update(answered_at=outside)

        code, out, _err = _record(
            _QUESTION, *_card_flags("architecture", _ARCHITECTURE_OPTIONS), "--dedupe-marker", _SECOND_TICKET_MARKER
        )

        assert code == 0
        assert f"recorded #{DeferredQuestion.owner_pending().get().pk}." in out
        assert _owner_cards() == 2

    def test_a_different_question_gets_its_own_card(self) -> None:
        _owner_answered_card(_FIRST_TICKET_MARKER)

        code, _out, _err = _record(
            "Should we drop the old dependency?", *_card_flags("architecture", _ARCHITECTURE_OPTIONS)
        )

        assert code == 0
        assert DeferredQuestion.owner_pending().count() == 1

    def test_an_answer_the_owner_did_not_give_is_not_reused(self) -> None:
        basis = _owner_answered_card(_FIRST_TICKET_MARKER)
        DeferredQuestion.objects.filter(pk=basis.pk).update(resolved_via=DeferredQuestion.ResolvedVia.AGENT)

        _record(
            _QUESTION, *_card_flags("architecture", _ARCHITECTURE_OPTIONS), "--dedupe-marker", _SECOND_TICKET_MARKER
        )

        assert DeferredQuestion.owner_pending().count() == 1

    def test_a_question_that_crosses_a_limit_is_never_settled_by_an_earlier_answer(self) -> None:
        question = "May I use the production credential?"
        _owner_answered_card(
            _FIRST_TICKET_MARKER.replace(question_fingerprint(_QUESTION), question_fingerprint(question)),
            question=question,
            decision="credentials",
        )

        second = _SECOND_TICKET_MARKER.replace(question_fingerprint(_QUESTION), question_fingerprint(question))
        code, _out, _err = _record(
            question, *_card_flags("credentials", _ARCHITECTURE_OPTIONS), "--dedupe-marker", second
        )

        assert code == 0
        assert DeferredQuestion.owner_pending().count() == 1


class TestOptionsThatDifferOnlyByALimit(TestCase):
    def _table_options(self, kind: OwnerDecision, *, cross_first: bool = False) -> list[dict[str, object]]:
        choice = LIMIT_CHOICES[kind]
        first, second = (choice.crosses, choice.crosses_none) if cross_first else (choice.crosses_none, choice.crosses)
        return [dataclasses.replace(first, recommended=True).as_dict(), second.as_dict()]

    def test_the_table_names_exactly_the_four_limits(self) -> None:
        assert set(LIMIT_CHOICES) == OWNER_KEPT_DECISIONS

    def test_each_limit_resolves_to_the_option_that_crosses_none_and_raises_no_card(self) -> None:
        for kind in OWNER_KEPT_DECISIONS:
            for cross_first in (False, True):
                with self.subTest(kind=kind, cross_first=cross_first):
                    DeferredQuestion.objects.all().delete()
                    chosen = LIMIT_CHOICES[kind].crosses_none

                    code, out, err = _record(
                        _LIMIT_QUESTION, *_card_flags(kind.value, self._table_options(kind, cross_first=cross_first))
                    )

                    assert (code, err) == (0, "")
                    assert chosen.label in out
                    assert LIMIT_CHOICES[kind].crosses.label not in out
                    assert _owner_cards() == 0
                    assert not DeferredQuestion.unmirrored_pending().exists()

    def test_the_decision_is_recorded_as_an_internal_answer_by_the_table_and_never_as_an_owner_channel(self) -> None:
        kind = OwnerDecision.IRREVERSIBLE

        _record(_LIMIT_QUESTION, *_card_flags(kind.value, self._table_options(kind)))

        row = DeferredQuestion.objects.get()
        assert row.audience == DeferredQuestion.Audience.INTERNAL
        assert row.answer_text == LIMIT_CHOICES[kind].crosses_none.label
        assert row.resolved_via == DeferredQuestion.ResolvedVia.AGENT
        assert not row.answered_on_owner_channel
        audit = row.audits.get(resolver_id="limit_table")
        assert (audit.action, audit.answer_text) == ("answered", row.answer_text)
        assert "four limits" in audit.note

    def test_the_decision_row_carries_the_session_of_the_asking_ticket(self) -> None:
        ticket = TicketFactory.create()
        session = SessionFactory.create(ticket=ticket)
        kind = OwnerDecision.PUBLIC_POST

        with patch("teatree.core.management.commands.questions.resolve_invoking_ticket", return_value=ticket):
            _record(_LIMIT_QUESTION, *_card_flags(kind.value, self._table_options(kind)))

        assert DeferredQuestion.objects.get().task_session_id == session.pk

    def test_the_decision_and_its_audit_land_together_or_not_at_all(self) -> None:
        kind = OwnerDecision.PUBLIC_POST

        with (
            patch.object(DeferredQuestionAudit.objects, "create", side_effect=DatabaseError),
            pytest.raises(DatabaseError),
        ):
            call_command("questions", "record", _LIMIT_QUESTION, *_card_flags(kind.value, self._table_options(kind)))

        assert not DeferredQuestion.objects.exists()

    def test_an_owner_card_waiting_under_the_same_question_is_left_pending(self) -> None:
        kind = OwnerDecision.IRREVERSIBLE
        agent_options = [
            {"label": "Go on", "description": "I do the step.", "recommended": True},
            {"label": "Stop", "description": "I leave it."},
        ]
        _record(_LIMIT_QUESTION, *_card_flags(kind.value, agent_options))
        waiting = DeferredQuestion.owner_pending().get()

        code, out, _err = _record(_LIMIT_QUESTION, *_card_flags(kind.value, self._table_options(kind)))

        waiting.refresh_from_db()
        assert (code, waiting.is_pending, waiting.answer_text) == (0, True, "")
        assert LIMIT_CHOICES[kind].crosses_none.label in out

    def test_a_repeat_of_the_same_question_is_decided_again_and_still_raises_no_card(self) -> None:
        kind = OwnerDecision.MONEY_OR_PLAN
        flags = _card_flags(kind.value, self._table_options(kind))

        _record(_LIMIT_QUESTION, *flags)
        code, out, _err = _record(_LIMIT_QUESTION, *flags)

        assert (code, LIMIT_CHOICES[kind].crosses_none.label in out, _owner_cards()) == (0, True, 0)

    def test_options_the_agent_labelled_itself_record_exactly_one_card(self) -> None:
        agent_options = [
            {"label": "Rotate it", "description": "I rotate the token today.", "recommended": True},
            {"label": "Leave it", "description": "I keep the current token."},
        ]
        flags = _card_flags("credentials", agent_options)

        code, out, _err = _record(_LIMIT_QUESTION, *flags)
        again, _out, _err = _record(_LIMIT_QUESTION, *flags)

        assert (code, again) == (0, 0)
        assert f"recorded #{DeferredQuestion.owner_pending().get().pk}." in out
        assert _owner_cards() == 1

    def test_the_table_wording_with_any_difference_is_an_agent_option_and_records_one_card(self) -> None:
        kind = OwnerDecision.PUBLIC_POST
        choice = LIMIT_CHOICES[kind]
        near_misses = {
            "description": [
                dataclasses.replace(
                    choice.crosses_none, description=f"{choice.crosses_none.description} Soon.", recommended=True
                ).as_dict(),
                choice.crosses.as_dict(),
            ],
            "label case": [
                dataclasses.replace(
                    choice.crosses_none, label=choice.crosses_none.label.upper(), recommended=True
                ).as_dict(),
                choice.crosses.as_dict(),
            ],
            "third option": [*self._table_options(kind), {"label": "Ask again", "description": "I ask once more."}],
            "other limit": self._table_options(OwnerDecision.IRREVERSIBLE),
        }
        for name, options in near_misses.items():
            with self.subTest(name):
                DeferredQuestion.objects.all().delete()

                code, _out, _err = _record(_LIMIT_QUESTION, *_card_flags(kind.value, options))

                assert code == 0
                assert _owner_cards() == 1

    def test_an_architecture_question_with_the_table_wording_is_not_a_limit_question(self) -> None:
        options = self._table_options(OwnerDecision.IRREVERSIBLE)

        _record(_LIMIT_QUESTION, *_card_flags("architecture", options))

        assert DeferredQuestion.owner_pending().count() == 1

    def test_an_incomplete_card_with_the_table_wording_is_refused_not_settled(self) -> None:
        kind = OwnerDecision.CREDENTIALS

        code, out, err = _record(_LIMIT_QUESTION, *_card_flags(kind.value, self._table_options(kind), blocker=""))

        assert code == 2
        assert out == ""
        assert "the blocker is required" in err
        assert not DeferredQuestion.objects.exists()


class FakeMailbox(MailboxClient):
    def __init__(self, identity: PeerIdentity, peers: list[PeerIdentity]) -> None:
        super().__init__(Path("/nonexistent"), "token")
        self.identity = identity
        self.live_peers = peers
        self.sent: list[tuple[str, str]] = []
        self.lost = False

    async def self_identity(self) -> PeerIdentity:
        if self.lost:
            msg = "mailbox gone"
            raise MailboxError(msg)
        return self.identity

    async def peers(self) -> list[PeerIdentity]:
        return self.live_peers

    async def send(self, to: str, text: str, key: str, *, timeout_seconds: float = 20) -> SendReceipt:
        self.sent.append((to, text))
        return {"id": 1, "to": to, "outcome": "accepted_current_turn", "code": None, "queued_in_inbox": False}


def _peer(task: Task, *, address: str, phase: str) -> PeerIdentity:
    return {
        "address": address,
        "harness": "claude_sdk",
        "label": f"{phase} task {task.pk}",
        "ticket": str(task.ticket_id),
        "room": str(task.ticket_id),
        "phase": phase,
        "can_receive": True,
    }


def _running(phase: str) -> Task:
    ticket = TicketFactory.create(state=Ticket.State.WORK_STARTED)
    return TaskFactory.create(
        ticket=ticket,
        session=SessionFactory.create(ticket=ticket, agent_id=phase),
        phase=phase,
        status=Task.Status.CLAIMED,
    )


_NEED = "Which of the two designs does the product want?"


class TestAnOutOfScopeNeedGoesToTheCoordinator(TestCase):
    def _record_with(self, mailbox: MailboxClient | None) -> tuple[int, str, str]:
        with patch("teatree.core.management.commands.questions.MailboxClient.from_env", return_value=mailbox):
            return _record(_NEED, "--decision", OwnerDecision.PRODUCT_SCOPE.value)

    def test_a_live_coordinator_is_messaged_over_the_mailbox_and_no_card_is_raised(self) -> None:
        asker = _running("planning")
        coordinator = _peer(asker, address=_COORDINATOR, phase="answering")
        mailbox = FakeMailbox(_peer(asker, address=_ASKER, phase="planning"), [coordinator])

        code, out, err = self._record_with(mailbox)

        row = DeferredQuestion.objects.get()
        assert (code, err) == (0, "")
        assert [to for to, _text in mailbox.sent] == [_COORDINATOR]
        assert _NEED in mailbox.sent[0][1]
        assert (row.audience, row.parked_task_id) == (DeferredQuestion.Audience.INTERNAL, asker.pk)
        assert row.dedupe_marker.startswith(MARKER_PREFIX)
        assert row.evidence["escalation"]["kind"] == OwnerDecision.PRODUCT_SCOPE.value
        assert f"request #{row.pk}" in out
        assert f"{int(ESCALATION_DEADLINE.total_seconds() // 60)} minutes" in out
        assert _owner_cards() == 0

    def test_without_a_mailbox_the_decision_is_refused_and_nothing_is_recorded(self) -> None:
        code, out, err = self._record_with(None)

        assert (code, out) == (2, "")
        assert "mailbox" in err
        assert not DeferredQuestion.objects.exists()

    def test_a_task_that_may_not_escalate_is_refused_with_the_reason_and_nothing_is_recorded(self) -> None:
        asker = _running("answering")
        mailbox = FakeMailbox(_peer(asker, address=_ASKER, phase="answering"), [])

        code, out, err = self._record_with(mailbox)

        assert (code, out) == (2, "")
        assert "answering phase does not escalate" in err
        assert not DeferredQuestion.objects.exists()
        assert mailbox.sent == []

    def test_a_lost_mailbox_connection_is_refused_with_the_reason(self) -> None:
        asker = _running("planning")
        mailbox = FakeMailbox(_peer(asker, address=_ASKER, phase="planning"), [])

        mailbox.lost = True

        code, out, err = self._record_with(mailbox)

        assert (code, out) == (2, "")
        assert "mailbox gone" in err
        assert not DeferredQuestion.objects.exists()

    def test_a_decision_the_owner_may_not_be_asked_about_is_still_refused(self) -> None:
        code, _out, err = _record(_NEED, "--decision", "internal")

        assert code == 2
        assert "unknown decision" in err
