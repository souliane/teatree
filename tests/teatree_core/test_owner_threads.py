"""The shared owner-DM-thread seam — one identity, one resolution record (#3642/#3658)."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import DeferredQuestion
from teatree.core.owner_threads import AUTO_RESOLVE_MAX_AGE, open_owner_threads, resolve_owner_thread


def _question(*, text: str = "ship it?", age: timedelta = timedelta(), **kw: object) -> DeferredQuestion:
    row = DeferredQuestion.record(text, slack_channel="D0OWNER", slack_ts="1779990001.000001", **kw)
    if age:
        DeferredQuestion.objects.filter(pk=row.pk).update(created_at=timezone.now() - age)
        row.refresh_from_db()
    return row


class TestOpenOwnerThreads(TestCase):
    def test_a_pending_owner_question_is_an_open_thread(self) -> None:
        row = _question()
        threads = open_owner_threads()
        assert [t.question.pk for t in threads] == [row.pk]
        assert threads[0].channel == "D0OWNER"
        assert threads[0].ts == "1779990001.000001"

    def test_a_resolved_question_is_not_an_open_thread(self) -> None:
        row = _question()
        DeferredQuestion.consume(row.pk, answer="yes")
        assert open_owner_threads() == ()

    def test_an_internal_question_is_never_an_owner_thread(self) -> None:
        _question(audience=DeferredQuestion.Audience.INTERNAL)
        assert open_owner_threads() == ()

    def test_a_young_thread_is_included_even_before_the_watermark(self) -> None:
        _question(age=timedelta(hours=2))
        since = timezone.now() - timedelta(minutes=30)
        assert len(open_owner_threads(since=since)) == 1

    def test_an_old_thread_before_the_watermark_is_out_of_this_pass(self) -> None:
        _question(age=timedelta(days=3))
        since = timezone.now() - timedelta(hours=1)
        assert open_owner_threads(since=since) == ()


class TestOwnerThreadCoordinates(TestCase):
    def test_created_at_reads_the_underlying_question_timestamp(self) -> None:
        row = _question()
        thread = open_owner_threads()[0]
        assert thread.created_at == row.created_at


class TestAutoResolvable(TestCase):
    def test_a_thread_younger_than_the_floor_is_auto_resolvable(self) -> None:
        _question(age=AUTO_RESOLVE_MAX_AGE - timedelta(minutes=1))
        assert open_owner_threads()[0].auto_resolvable() is True

    def test_a_thread_older_than_the_floor_is_not(self) -> None:
        _question(age=AUTO_RESOLVE_MAX_AGE + timedelta(minutes=1))
        assert open_owner_threads()[0].auto_resolvable() is False


class TestResolveOwnerThread(TestCase):
    def test_resolution_closes_the_thread_with_its_reason(self) -> None:
        row = _question()
        thread = open_owner_threads()[0]

        assert resolve_owner_thread(thread, reason="subject merged") is True

        row.refresh_from_db()
        assert row.status == DeferredQuestion.STATUS_DISMISSED
        assert row.dismissed_reason == "subject merged"
        assert open_owner_threads() == ()

    def test_resolution_never_overwrites_an_answer_the_owner_gave(self) -> None:
        row = _question()
        thread = open_owner_threads()[0]
        DeferredQuestion.consume(row.pk, answer="yes")
        thread.question.refresh_from_db()

        assert resolve_owner_thread(thread, reason="looked stale") is False

        row.refresh_from_db()
        assert row.status == DeferredQuestion.STATUS_ANSWERED
        assert row.answer_text == "yes"
