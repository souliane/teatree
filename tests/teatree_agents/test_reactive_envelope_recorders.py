"""The scanning-news envelope is recorded AND delivered as a Slack digest (#3669, #1391)."""

from unittest import mock

from django.test import TestCase

from teatree.agents.reactive_envelope_recorders import record_reactive_envelopes
from teatree.core.models import DeferredQuestion, PendingArticleSuggestion, Session, Task, Ticket
from teatree.verification.url_check import UrlCheckResult, UrlCheckStatus

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
