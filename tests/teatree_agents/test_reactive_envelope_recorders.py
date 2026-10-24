"""The scanning-news envelope is recorded AND delivered as a Slack digest (#3669, #1391)."""

from unittest import mock

from django.test import TestCase

from teatree.agents.reactive_envelope_recorders import record_reactive_envelopes
from teatree.answer_handback import collect
from teatree.core.models import DeferredQuestion, PendingArticleSuggestion, Session, Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestionAudit
from teatree.verification.url_check import UrlCheckResult, UrlCheckStatus
from tests._owner_channel import assert_a_plain_card

_SUGGESTIONS = [
    {
        "title": "An agent eval harness",
        "url": "https://example.com/eval",
        "rationale": "mirrors src/teatree/eval/backends.py",
    },
    {"title": "Loop scheduling", "url": "https://example.com/loop", "rationale": "relevant to the tick cadence"},
]


def _news_task() -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, overlay="t3-teatree")
    session = Session.objects.create(ticket=ticket, agent_id="scanning-news")
    return Task.objects.create(ticket=ticket, session=session, phase="scanning_news")


class TestScanningNewsDigestDelivery(TestCase):
    def setUp(self) -> None:
        """Every cited URL resolves — this suite tests delivery, not URL verification."""
        resolving_urls = mock.patch(
            "teatree.core.models.pending_article_suggestion.check_url",
            side_effect=lambda url: UrlCheckResult(url=url, status=UrlCheckStatus.OK, http_status=200),
        )
        resolving_urls.start()
        self.addCleanup(resolving_urls.stop)

    def test_candidates_are_still_recorded_behind_the_ask_gate(self) -> None:
        task = _news_task()

        with mock.patch("teatree.agents.reactive_envelope_recorders.notify_user", return_value=True):
            record_reactive_envelopes(task, {"article_suggestions": _SUGGESTIONS}, phase="scanning_news")

        assert PendingArticleSuggestion.objects.count() == 2
        assert DeferredQuestion.objects.count() == 1

    def test_the_batch_question_names_no_asking_session_so_its_answer_is_never_posted(self) -> None:
        task = _news_task()
        Task.objects.filter(pk=task.pk).update(claimed_by_session="loop-driving-session")
        task.refresh_from_db()
        with mock.patch("teatree.agents.reactive_envelope_recorders.notify_user", return_value=True):
            record_reactive_envelopes(task, {"article_suggestions": _SUGGESTIONS}, phase="scanning_news")
        question = DeferredQuestion.objects.get()

        with self.captureOnCommitCallbacks(execute=True):
            DeferredQuestion.consume(question.pk, answer="approve both")

        question.refresh_from_db()
        assert question.session_id == ""
        assert question.applied_at is None
        assert collect("loop-driving-session") == []

    def test_the_digest_is_delivered_to_slack(self) -> None:
        task = _news_task()

        with mock.patch("teatree.agents.reactive_envelope_recorders.notify_user", return_value=True) as notify:
            record_reactive_envelopes(task, {"article_suggestions": _SUGGESTIONS}, phase="scanning_news")

        assert notify.call_count == 1
        text = notify.call_args.args[0]
        assert "<https://example.com/eval|An agent eval harness>" in text
        assert "`src/teatree/eval/backends.py`" in text

    def test_the_digest_is_idempotent_per_task(self) -> None:
        task = _news_task()

        with mock.patch("teatree.agents.reactive_envelope_recorders.notify_user", return_value=True) as notify:
            record_reactive_envelopes(task, {"article_suggestions": _SUGGESTIONS}, phase="scanning_news")

        assert notify.call_args.kwargs["idempotency_key"] == f"news-digest-{task.pk}"

    def test_a_failed_slack_post_never_loses_the_recorded_candidates(self) -> None:
        task = _news_task()

        with mock.patch(
            "teatree.agents.reactive_envelope_recorders.notify_user",
            side_effect=RuntimeError("slack down"),
        ):
            record_reactive_envelopes(task, {"article_suggestions": _SUGGESTIONS}, phase="scanning_news")

        assert PendingArticleSuggestion.objects.count() == 2

    def test_an_empty_scan_posts_no_digest(self) -> None:
        task = _news_task()

        with mock.patch("teatree.agents.reactive_envelope_recorders.notify_user") as notify:
            record_reactive_envelopes(task, {"article_suggestions": []}, phase="scanning_news")

        notify.assert_not_called()

    def test_an_empty_rendered_digest_posts_nothing_but_keeps_the_candidates(self) -> None:
        # If the digest renders to empty text there is nothing to DM, so the post is
        # skipped — yet the candidates the ask-gate needs are already persisted.
        task = _news_task()

        with (
            mock.patch("teatree.agents.reactive_envelope_recorders.render_digest", return_value=""),
            mock.patch("teatree.agents.reactive_envelope_recorders.notify_user") as notify,
        ):
            record_reactive_envelopes(task, {"article_suggestions": _SUGGESTIONS}, phase="scanning_news")

        notify.assert_not_called()
        assert PendingArticleSuggestion.objects.count() == 2


def _task_in(phase: str) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, overlay="acme")
    return Task.objects.create(
        ticket=ticket, session=Session.objects.create(ticket=ticket, agent_id=phase), phase=phase
    )


def _draft(text: str) -> dict[str, object]:
    return {"answer": {"text": text, "thread_ref": "C0DEMOCHAN1/1700000000.000100"}}


class TestAnAnswerDraftIsAskedAsACard(TestCase):
    def test_a_clean_draft_is_quoted_on_a_card_that_recommends_posting_it(self) -> None:
        task = _task_in("answering")

        record_reactive_envelopes(task, _draft("Thanks, alice. The fix ships on Monday."), phase="answering")

        row = DeferredQuestion.objects.get()
        text = assert_a_plain_card(row, "C0DEMOCHAN1", "1700000000")
        assert row.parked_task_id == task.pk
        assert row.evidence["decision"] == "public_post"
        assert "> Thanks, alice. The fix ships on Monday." in text
        assert text.index("[Post it] (recommended)") < text.index("[Do not post]")

    def test_a_draft_that_fails_the_checks_is_withheld_on_its_task(self) -> None:
        task = _task_in("answering")

        record_reactive_envelopes(task, _draft("See #42 and acme_nightly for details."), phase="answering")

        assert not DeferredQuestion.owner_pending().exists()
        row = DeferredQuestion.objects.get()
        assert row.audience == DeferredQuestion.Audience.INTERNAL
        assert row.parked_task_id == task.pk
        assert row.dedupe_marker == f"answer-draft:{task.pk}"
        audit = DeferredQuestionAudit.objects.get(question=row, action="withheld")
        assert "#42" in audit.note
        assert "acme_nightly" in audit.note

    def test_a_200_word_draft_is_withheld_not_cut_short(self) -> None:
        task = _task_in("answering")

        record_reactive_envelopes(task, _draft(" ".join(["word"] * 200)), phase="answering")

        assert not DeferredQuestion.owner_pending().exists()
        assert "words; at most 120" in DeferredQuestionAudit.objects.get(action="withheld").note

    def test_a_second_pass_over_the_same_task_withholds_once(self) -> None:
        task = _task_in("answering")

        record_reactive_envelopes(task, _draft("See #42."), phase="answering")
        record_reactive_envelopes(task, _draft("See #42."), phase="answering")

        assert DeferredQuestion.objects.count() == 1
        assert DeferredQuestionAudit.objects.filter(action="withheld").count() == 1


class TestATriageBatchIsAskedAsACard(TestCase):
    def test_the_card_counts_the_suggestions_and_names_no_task_or_label(self) -> None:
        task = _task_in("triage_assessing")
        recommendations = [
            {"issue_url": "https://git.acme.example/widgets/issues/1", "verdict": "close", "rationale": "dupe"},
            {"issue_url": "https://git.acme.example/widgets/issues/2", "verdict": "keep", "rationale": "valid"},
        ]

        record_reactive_envelopes(task, {"triage_recommendations": recommendations}, phase="triage_assessing")

        row = DeferredQuestion.owner_pending().get()
        text = assert_a_plain_card(row, "needs-triage", "triage-batch", "/t3:")
        assert "2 open issue(s)" in text
        assert row.parked_task_id == task.pk
