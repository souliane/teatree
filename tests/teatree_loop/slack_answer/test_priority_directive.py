from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone
from teatree_loop.slack_answer.test_cycle import RecordingBackend

from teatree.core.admission_priority import ADMISSION_ORDER, admission_priority_annotations
from teatree.core.models import DeferredQuestion, DmContext, PendingChatInjection, Session, Task, ThreadBinding, Ticket
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.loop.slack_answer.priority_directive import parse_priority_directive
from teatree.types import RawAPIDict


def _ticket(number: int = 445) -> Ticket:
    return Ticket.objects.create(
        overlay="", issue_url=f"https://github.com/example/project/issues/{number}", short_description="Fix the build"
    )


def _row(text: str, ts: str, *, thread: str = "1.000", user: str = "UUSER") -> PendingChatInjection:
    row = PendingChatInjection.record(
        channel="D1", slack_ts=ts, text=text, context=DmContext(user_id=user, thread_ts=thread)
    )
    assert row is not None
    return row


def _backend() -> RecordingBackend:
    backend = RecordingBackend()
    backend.message_meta["1.000"] = {
        "ts": "1.000",
        "text": "<https://github.com/example/project/issues/445|#445 Fix the build>",
    }
    backend.message_meta["2.000"] = {"ts": "2.000", "thread_ts": "1.000"}
    backend.message_meta["3.000"] = {"ts": "3.000", "thread_ts": "1.000"}
    return backend


def _ack(_text: str) -> InboundReading:
    return InboundReading(
        intent=InboundIntent.FYI,
        answerable=False,
        work_summary="",
        source=ReadingSource.MODEL,
        rationale="test",
    )


class TestPriorityDirective(TestCase):
    def test_owner_directive_expedites_then_clears_with_verified_replies(self) -> None:
        ticket = _ticket()
        older_ticket = _ticket(446)
        older_task = Task.objects.create(
            ticket=older_ticket,
            session=Session.objects.create(ticket=older_ticket, agent_id="older"),
            phase="planning",
        )
        priority_task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, agent_id="priority"), phase="planning"
        )

        def ranked() -> list[int]:
            qs = Task.objects.filter(pk__in=[older_task.pk, priority_task.pk]).annotate(
                **admission_priority_annotations()
            )
            return list(qs.order_by(*ADMISSION_ORDER).values_list("pk", flat=True))

        assert ranked() == [older_task.pk, priority_task.pk]
        backend = _backend()
        reader = Mock(side_effect=AssertionError("pure directive reached the reader"))
        _row("this is urgent", "2.000")

        first = run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=reader)

        ticket.refresh_from_db()
        assert first.errors == 0
        assert ticket.expedited is True
        assert ranked() == [priority_task.pk, older_task.pk]
        assert len(backend.replies) == 1
        assert "#445" in backend.replies[0][2]
        assert "after reviews" in backend.replies[0][2]
        assert "No merge gate" in backend.replies[0][2]
        reader.assert_not_called()

        _row("not urgent", "3.000")
        second = run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=reader)

        ticket.refresh_from_db()
        assert second.errors == 0
        assert ticket.expedited is False
        assert ranked() == [older_task.pk, priority_task.pk]
        assert len(backend.replies) == 2
        assert "normally again" in backend.replies[1][2]
        reader.assert_not_called()

    def test_failed_post_retry_cannot_reverse_a_later_directive(self) -> None:
        ticket = _ticket()
        backend = _backend()
        original_post = backend.post_reply

        def post_except_earlier(*, channel: str, ts: str, text: str) -> RawAPIDict:
            if ts == "2.000":
                message = "Slack post unavailable"
                raise RuntimeError(message)
            return original_post(channel=channel, ts=ts, text=text)

        earlier = _row("urgent", "2.000")
        later = _row("not urgent", "3.000")
        with patch.object(backend, "post_reply", side_effect=post_except_earlier):
            run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=_ack)
        earlier.refresh_from_db()
        later.refresh_from_db()
        assert ticket.__class__.objects.get(pk=ticket.pk).expedited is False
        assert earlier.loop_replied_at is None
        assert later.loop_replied_at is not None
        assert len(backend.replies) == 1

        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=_ack)

        ticket.refresh_from_db()
        assert ticket.expedited is False
        assert len(backend.replies) == 2
        assert "superseded" in backend.replies[1][2]

    def test_unacknowledged_post_is_found_on_retry_without_a_second_reply(self) -> None:
        ticket = _ticket()
        backend = _backend()
        backend.read_after_post = False
        row = _row("urgent", "2.000")
        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=_ack)
        row.refresh_from_db()
        assert ticket.__class__.objects.get(pk=ticket.pk).expedited is True
        assert row.loop_replied_at is None
        assert len(backend.replies) == 1

        backend.read_after_post = True
        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=_ack)
        row.refresh_from_db()
        assert row.loop_replied_at is not None
        assert len(backend.replies) == 1

    def test_ambiguous_message_keeps_priority_and_routes_normally(self) -> None:
        ticket = _ticket()
        backend = _backend()
        reader = Mock(side_effect=_ack)
        for index, text in enumerate(
            ["is it urgent", "don't wait on this hotfix", "fix step #1 asap", "urgent but can wait"], start=2
        ):
            _row(text, f"{index}.000")
            run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=reader)
            ticket.refresh_from_db()
            assert ticket.expedited is False
            reader.assert_called_once()
            reader.reset_mock()

    def test_non_owner_cannot_expedite(self) -> None:
        ticket = _ticket()
        backend = _backend()
        _row("urgent", "2.000", user="UOTHER")

        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=_ack)

        ticket.refresh_from_db()
        assert ticket.expedited is False
        assert backend.replies == []
        assert "zap" not in [reaction[2] for reaction in backend.reactions]

    def test_explicit_issue_reference_expedites_only_unique_unsettled_issue(self) -> None:
        selected = _ticket()
        backend = RecordingBackend()
        _row("#445 urgent", "2.000", thread="")
        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=_ack)
        selected.refresh_from_db()
        assert selected.expedited is True

        collision = Ticket.objects.create(overlay="", issue_url="https://github.com/other/project/issues/446")
        other = Ticket.objects.create(overlay="", issue_url="https://github.com/example/project/issues/446")
        _row("#446 urgent", "3.000", thread="")
        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=_ack)
        collision.refresh_from_db()
        other.refresh_from_db()
        assert collision.expedited is False
        assert other.expedited is False

    def test_qualified_references_and_bound_thread_select_tickets(self) -> None:
        qualified = _ticket()
        linked = _ticket(446)
        bound = _ticket(447)
        backend = RecordingBackend()
        backend.message_meta["4.000"] = {"ts": "4.000", "thread_ts": "1.000"}
        ThreadBinding.bind(ticket=bound, channel="D1", thread_ts="1.000")
        _row("example/project#445 urgent", "2.000", thread="")
        _row("<https://github.com/example/project/issues/446|#446 Fix the build> urgent", "3.000", thread="")
        _row("urgent", "4.000")

        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=_ack)

        for ticket in (qualified, linked, bound):
            ticket.refresh_from_db()
            assert ticket.expedited is True
        assert len(backend.replies) == 3

    def test_answered_parked_question_thread_selects_its_ticket(self) -> None:
        ticket = _ticket()
        task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, agent_id="parked"), phase="coding"
        )
        DeferredQuestion.objects.create(
            question="Which path?",
            audience=DeferredQuestion.Audience.INTERNAL,
            slack_channel="D1",
            slack_ts="1.000",
            parked_task=task,
            answer_text="the first path",
            answered_at=timezone.now(),
        )
        backend = _backend()
        _row("urgent", "2.000")

        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=_ack)

        ticket.refresh_from_db()
        assert ticket.expedited is True
        assert len(backend.replies) == 1

    def test_cue_and_ambiguity_matrix(self) -> None:
        on = [
            "urgent",
            "urgently",
            "asap",
            "a.s.a.p.",
            "hotfix",
            "hot-fix",
            "top priority",
            "highest priority",
            "blocker",
            "release blocker",
        ]
        off = [
            "not urgent",
            "non-urgent",
            "no blocker",
            "never urgent",
            "no longer a blocker",
            "isn't urgent",
            "later",
            "no rush",
            "can wait",
            "low priority",
            "deprioritize",
        ]
        ambiguous = [
            "urgent but not urgent",
            "urgent but can wait",
            "don't wait on this hotfix",
            "I don't think this is urgent",
            "nothing is more urgent",
            "I'll check later",
            "the blocker is the flaky test",
            "is #445 urgent",
            "is it urgent",
            "is this a hotfix",
            "was it a blocker",
            "why is this a blocker",
            "do you think #445 is urgent",
            "<https://example.org/urgent|urgent>",
        ]
        for phrase in on:
            with self.subTest(phrase=phrase):
                directive = parse_priority_directive(phrase)
                assert directive is not None
                assert directive.enabled
                assert directive.only
        for phrase in off:
            with self.subTest(phrase=phrase):
                directive = parse_priority_directive(phrase)
                assert directive is not None
                assert not directive.enabled
                assert directive.only
        for phrase in ambiguous:
            with self.subTest(phrase=phrase):
                assert parse_priority_directive(phrase) is None

    def test_mixed_request_changes_priority_and_continues_normal_routing(self) -> None:
        ticket = _ticket()
        backend = _backend()
        reader = Mock(side_effect=_ack)
        _row("this is urgent. please also fix the flaky test", "2.000")

        report = run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=reader)

        ticket.refresh_from_db()
        assert report.errors == 0
        assert ticket.expedited is True
        assert "zap" in [reaction[2] for reaction in backend.reactions]
        reader.assert_called_once()

    def test_no_context_or_ineligible_ticket_preserves_routing(self) -> None:
        backend = RecordingBackend()
        reader = Mock(side_effect=_ack)
        _row("urgent", "2.000", thread="")
        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=reader)
        reader.assert_called_once()

        settled = _ticket()
        settled.state = Ticket.State.DELIVERED
        settled.save(update_fields=["state"])
        reader.reset_mock()
        _row("#445 urgent", "3.000", thread="")
        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=reader)
        settled.refresh_from_db()
        assert settled.expedited is False
        reader.assert_called_once()

    def test_pr_opened_ticket_is_settled_for_priority_selection(self) -> None:
        ticket = _ticket()
        ticket.state = Ticket.State.PR_OPENED
        ticket.save(update_fields=["state"])
        backend = RecordingBackend()
        reader = Mock(side_effect=_ack)
        _row("#445 urgent", "2.000", thread="")

        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=reader)

        ticket.refresh_from_db()
        assert ticket.expedited is False
        reader.assert_called_once()

    def test_two_directives_in_one_batch_keep_their_order(self) -> None:
        ticket = _ticket()
        backend = _backend()
        _row("urgent", "2.000")
        _row("not urgent", "3.000")

        report = run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=_ack)

        ticket.refresh_from_db()
        assert report.errors == 0
        assert ticket.expedited is False
        assert len(backend.replies) == 2

    def test_multiple_tickets_require_a_pure_directive(self) -> None:
        first = _ticket()
        second = _ticket(446)
        backend = RecordingBackend()
        _row("#445 #446 urgent", "2.000", thread="")

        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=_ack)

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.expedited is True
        assert second.expedited is True
        assert len(backend.replies) == 1
        assert "#445" in backend.replies[0][2]
        assert "#446" in backend.replies[0][2]

    def test_question_id_reply_never_selects_same_numbered_ticket(self) -> None:
        question = DeferredQuestion.objects.create(
            question="What should we do?",
            audience=DeferredQuestion.Audience.INTERNAL,
            slack_channel="D1",
            slack_ts="1.000",
        )
        ticket = _ticket(question.pk)
        backend = _backend()
        _row(f"#{question.pk} urgent", "2.000")

        run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend, reader=_ack)

        ticket.refresh_from_db()
        question.refresh_from_db()
        assert ticket.expedited is False
        assert question.answer_text == "urgent"
