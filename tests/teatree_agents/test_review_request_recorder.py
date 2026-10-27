"""A requesting_review run on the read-only executor drafts; only the worker sends, from its own rows."""

from unittest.mock import patch

from django.test import TestCase

from teatree.agents.attempt_recorder import AttemptUsage, record_result_envelope
from teatree.agents.review_request_recorder import NOT_SENT_PREFIX, record_returned_review_request
from teatree.core.modelkit.forge_readability import LiveHeadRead
from teatree.core.models import PullRequest, Session, Task, TaskAttempt, Ticket

_OWN_URL = "https://github.com/example-org/example-repo/pull/7"
_OTHER_URL = "https://github.com/example-org/other-repo/pull/9"
_HEAD = "a" * 40


class _Poster:
    def __init__(self, verdict: dict[str, object] | None = None) -> None:
        self.verdict = verdict or {"action": "post", "permalink": "https://chat.example/p1"}
        self.calls: list[dict[str, str]] = []

    def __call__(self, **kwargs: str) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.verdict


class TestTheWorkerSendsTheDraft(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, overlay="t3-teatree")
        PullRequest.objects.create(ticket=self.ticket, url=_OWN_URL, repo="example-org/example-repo", iid="7")
        self.task = Task.objects.create(
            ticket=self.ticket, session=Session.objects.create(ticket=self.ticket), phase="requesting_review"
        )
        self.poster = _Poster()
        head = patch(
            "teatree.agents.review_request_recorder.live_head_at",
            return_value=LiveHeadRead(sha=_HEAD, unreadable=False),
        )
        head.start()
        self.addCleanup(head.stop)

    def _record(self, draft: object) -> str:
        return record_returned_review_request(
            self.task, {"review_request": draft}, phase="requesting_review", poster=self.poster
        )

    def test_a_ready_draft_posts_the_tickets_own_pull_request_at_the_workers_live_head(self) -> None:
        refusal = self._record({"ready": True, "pr_url": _OWN_URL, "title": "feat(x):\n add y"})

        assert refusal == ""
        assert self.poster.calls == [
            {"mr_url": _OWN_URL, "title": "feat(x): add y", "ticket_id": str(self.ticket.pk), "head_sha": _HEAD}
        ]

    def test_a_pull_request_url_the_ticket_does_not_own_posts_nothing(self) -> None:
        refusal = self._record({"ready": True, "pr_url": _OTHER_URL, "title": "feat: elsewhere"})

        assert refusal.startswith(NOT_SENT_PREFIX)
        assert self.poster.calls == []

    def test_another_tickets_open_pull_request_is_not_this_tickets_to_request(self) -> None:
        elsewhere = Ticket.objects.create(role=Ticket.Role.AUTHOR, overlay="t3-teatree")
        PullRequest.objects.create(ticket=elsewhere, url=_OTHER_URL, repo="example-org/other-repo", iid="9")

        refusal = self._record({"ready": True, "pr_url": _OTHER_URL, "title": "feat: elsewhere"})

        assert refusal.startswith(NOT_SENT_PREFIX)
        assert self.poster.calls == []

    def test_chat_markup_in_the_title_reaches_the_poster_escaped(self) -> None:
        title = "feat: <!channel> <!subteam^S123> <@U123> <https://evil.example|label> a&b"

        assert self._record({"ready": True, "title": title}) == ""

        (call,) = self.poster.calls
        assert call["title"] == (
            "feat: &lt;!channel&gt; &lt;!subteam^S123&gt; &lt;@U123&gt; &lt;https://evil.example|label&gt; a&amp;b"
        )
        assert not {"<", ">"} & set(call["title"])

    def test_a_draft_returned_while_stopping_for_the_owner_posts_nothing(self) -> None:
        draft = {"ready": True, "pr_url": _OWN_URL, "title": "feat: x"}

        refusal = record_returned_review_request(
            self.task,
            {"needs_user_input": True, "review_request": draft},
            phase="requesting_review",
            poster=self.poster,
        )

        assert (refusal, self.poster.calls) == ("", [])

    def test_a_single_open_pull_request_needs_no_url(self) -> None:
        assert self._record({"ready": True}) == ""
        assert [call["mr_url"] for call in self.poster.calls] == [_OWN_URL]

    def test_a_draft_that_is_not_ready_posts_nothing_and_names_its_reasons(self) -> None:
        refusal = self._record({"ready": False, "reasons": ["CI is red"]})

        assert (refusal.startswith(NOT_SENT_PREFIX), "CI is red" in refusal, self.poster.calls) == (True, True, [])

    def test_a_refused_post_is_not_recorded_as_sent(self) -> None:
        self.poster.verdict = {"action": "refused", "reason": "on_behalf_not_approved"}

        assert self._record({"ready": True}) == f"{NOT_SENT_PREFIX}on_behalf_not_approved"

    def test_a_suppressed_duplicate_is_a_landed_outcome(self) -> None:
        self.poster.verdict = {"action": "suppress", "reason": "already_posted"}

        assert self._record({"ready": True}) == ""

    def test_an_unreadable_live_head_posts_nothing(self) -> None:
        with patch(
            "teatree.agents.review_request_recorder.live_head_at", return_value=LiveHeadRead(sha="", unreadable=True)
        ):
            refusal = self._record({"ready": True})

        assert (refusal.startswith(NOT_SENT_PREFIX), self.poster.calls) == (True, [])

    def test_another_phase_or_an_absent_draft_is_a_no_op(self) -> None:
        assert record_returned_review_request(self.task, {}, phase="requesting_review", poster=self.poster) == ""
        assert (
            record_returned_review_request(
                self.task, {"review_request": {"ready": True}}, phase="coding", poster=self.poster
            )
            == ""
        )
        assert self.poster.calls == []


class TestTheExecutorMustReturnTheDraft(TestCase):
    def setUp(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, overlay="t3-teatree")
        self.task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket), phase="requesting_review"
        )
        self.task.claim(claimed_by="loop-slot")

    def _record(self, harness: str) -> TaskAttempt:
        return record_result_envelope(
            self.task,
            {"summary": "asked for review"},
            phase="requesting_review",
            usage=AttemptUsage(selected_harness=harness),
        )

    def test_a_codex_run_with_no_draft_is_refused(self) -> None:
        attempt = self._record("codex_app_server")

        assert "must return `review_request`" in attempt.error

    def test_a_claude_run_with_a_summary_only_envelope_still_records(self) -> None:
        assert self._record("claude_sdk").error == ""

    def test_a_claude_run_never_posts_a_review_request_draft(self) -> None:
        with patch("teatree.agents.review_request_recorder.call_command_streamed") as post:
            attempt = record_result_envelope(
                self.task,
                {"summary": "asked", "review_request": {"ready": True}},
                phase="requesting_review",
                usage=AttemptUsage(selected_harness="claude_sdk"),
            )

        assert attempt.error == ""
        post.assert_not_called()
