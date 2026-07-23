"""The hourly owner-DM hygiene pass — resolve only what is provably handled (#3658)."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import DeferredQuestion, PullRequest, Ticket
from teatree.core.owner_dm_sweep import SweepSeams, run_sweep
from teatree.core.owner_threads import AUTO_RESOLVE_MAX_AGE, open_owner_threads

_PR_URL = "https://github.com/souliane/teatree/pull/1234"


def _thread(text: str, *, age: timedelta = timedelta(minutes=5), ts: str = "1779990001.000001") -> DeferredQuestion:
    row = DeferredQuestion.record(text, slack_channel="D0OWNER", slack_ts=ts)
    DeferredQuestion.objects.filter(pk=row.pk).update(created_at=timezone.now() - age)
    row.refresh_from_db()
    return row


class TestOwnerAlreadyAnswered(TestCase):
    def test_a_thread_the_owner_replied_in_is_resolved(self) -> None:
        row = _thread("Ship it?")

        result = run_sweep(seams=SweepSeams(owner_replied=lambda _t: True))

        assert result.resolved == 1
        row.refresh_from_db()
        assert row.status == DeferredQuestion.STATUS_DISMISSED

    def test_an_unanswered_thread_is_left_open(self) -> None:
        row = _thread("Ship it?")

        result = run_sweep(seams=SweepSeams(owner_replied=lambda _t: False))

        assert result.resolved == 0
        assert result.left_open == 1
        row.refresh_from_db()
        assert row.status == DeferredQuestion.STATUS_PENDING

    def test_with_no_backend_the_rule_is_skipped_not_guessed(self) -> None:
        _thread("Ship it?")

        assert run_sweep(seams=SweepSeams(owner_replied=None)).resolved == 0


class TestSubjectClosed(TestCase):
    def test_a_thread_about_a_merged_pull_request_is_resolved(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.MERGED)
        PullRequest.objects.create(ticket=ticket, url=_PR_URL, repo="souliane/teatree", iid="1234")
        row = _thread(f"Should I rebase {_PR_URL}?")

        assert run_sweep().resolved == 1
        row.refresh_from_db()
        assert row.dismissed_reason == "the thread's subject is closed"

    def test_a_thread_about_an_open_pull_request_is_left_alone(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.IN_REVIEW)
        PullRequest.objects.create(ticket=ticket, url=_PR_URL, repo="souliane/teatree", iid="1234")
        _thread(f"Should I rebase {_PR_URL}?")

        assert run_sweep().resolved == 0

    def test_a_thread_naming_no_subject_is_left_alone(self) -> None:
        _thread("What should I do next?")

        assert run_sweep().resolved == 0

    def test_an_untracked_reference_is_not_assumed_closed(self) -> None:
        _thread(f"Should I rebase {_PR_URL}?")

        assert run_sweep().resolved == 0


class TestDuplicateThreads(TestCase):
    def test_the_newer_duplicate_is_retired_and_the_oldest_kept(self) -> None:
        oldest = _thread("Ship it?", age=timedelta(hours=3), ts="1779990001.000001")
        newer = _thread("  SHIP IT?  ", age=timedelta(minutes=5), ts="1779990002.000001")

        result = run_sweep()

        assert result.resolved == 1
        oldest.refresh_from_db()
        newer.refresh_from_db()
        assert oldest.status == DeferredQuestion.STATUS_PENDING
        assert newer.status == DeferredQuestion.STATUS_DISMISSED

    def test_two_different_questions_are_both_kept(self) -> None:
        _thread("Ship it?", age=timedelta(hours=3), ts="1779990001.000001")
        _thread("Revert it?", age=timedelta(minutes=5), ts="1779990002.000001")

        assert run_sweep().resolved == 0


class TestTheAgeFloor(TestCase):
    def test_a_thread_older_than_a_day_is_untouched_even_when_answered(self) -> None:
        row = _thread("Ship it?", age=AUTO_RESOLVE_MAX_AGE + timedelta(hours=1))

        result = run_sweep(seams=SweepSeams(owner_replied=lambda _t: True))

        assert result.resolved == 0
        row.refresh_from_db()
        assert row.status == DeferredQuestion.STATUS_PENDING
        # Still in the queue, so the resurfacing side keeps raising it in this thread.
        assert [t.question.pk for t in open_owner_threads()] == [row.pk]


class TestSilence(TestCase):
    def test_a_pass_with_nothing_to_do_reports_silence(self) -> None:
        result = run_sweep()

        assert result.silent is True
        assert result == run_sweep()

    def test_a_pass_that_resolved_something_is_not_silent(self) -> None:
        _thread("Ship it?")

        assert run_sweep(seams=SweepSeams(owner_replied=lambda _t: True)).silent is False
