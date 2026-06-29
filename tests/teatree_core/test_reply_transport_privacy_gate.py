"""The #1295 publication privacy gate wired at the reply-transport chokepoint.

A GitHub PR comment / GitLab MR note posted on the user's behalf carries its
body to a repo that may be PUBLIC, so ``_BaseReplier`` privacy-scans the body
via ``scan_outbound_text`` before the wire call and FAILS CLOSED on a finding.
A bot→user ``post_dm`` is never scanned. These tests inject the overlay's
``public_repos`` + redact rules (which live in the operator's private config in
production) and assert the chokepoint blocks a leaking body and passes a clean
one — they fail if the scan is not wired into ``_send`` / ``redeliver``.
"""

from collections.abc import Sequence
from unittest import mock
from unittest.mock import MagicMock

import pytest
from django.test import TestCase

from teatree.core.gates import privacy_gate
from teatree.core.models import IncomingEvent, ReplyDispatch
from teatree.core.reply_transport import GitHubReplier, GitLabReplier, PublicationPrivacyBlockedError, SlackReplier
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate

GITHUB_REPO = "owner/pub-repo"
GITLAB_REPO = "group/pub-project"
REDACT = "SECRETCORP"


@pytest.fixture(autouse=True)
def _no_on_behalf_gate(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    disable_on_behalf_gate(tmp_path_factory, monkeypatch)


def _event(source: str, *, channel_ref: str, thread_ref: str, key: str) -> IncomingEvent:
    return IncomingEvent.objects.create(
        source=source,
        actor="U_ALICE",
        channel_ref=channel_ref,
        thread_ref=thread_ref,
        body="originating message",
        idempotency_key=key,
    )


class TestReplyTransportPrivacyGate(TestCase):
    def _inject_rules(
        self,
        public_repos: Sequence[str],
        *,
        redact: Sequence[str] = (),
        block: Sequence[str] = (),
    ) -> None:
        patcher = mock.patch.object(
            privacy_gate,
            "_overlay_publication_rules",
            return_value=(list(public_repos), list(redact), list(block)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_github_pr_comment_blocked_on_redact_term(self) -> None:
        self._inject_rules([GITHUB_REPO], redact=[REDACT])
        event = _event(IncomingEvent.Source.GITHUB, channel_ref=GITHUB_REPO, thread_ref="17", key="gh:leak")
        host = MagicMock()

        dispatch = GitHubReplier(host=host).post_comment(
            event=event,
            target_ref=GITHUB_REPO,
            body=f"This PR review leaks {REDACT} internals.",
            idempotency_key="gh:leak:c",
        )

        assert dispatch.status == ReplyDispatch.Status.FAILED
        assert "privacy" in dispatch.error_message.lower()
        host.post_pr_comment.assert_not_called()

    def test_github_pr_comment_blocked_on_builtin_quote_anchor(self) -> None:
        self._inject_rules([GITHUB_REPO])
        event = _event(IncomingEvent.Source.GITHUB, channel_ref=GITHUB_REPO, thread_ref="17", key="gh:quote")
        host = MagicMock()

        dispatch = GitHubReplier(host=host).post_comment(
            event=event,
            target_ref=GITHUB_REPO,
            body="Posting what the user said verbatim here.",
            idempotency_key="gh:quote:c",
        )

        assert dispatch.status == ReplyDispatch.Status.FAILED
        host.post_pr_comment.assert_not_called()

    def test_github_pr_comment_clean_body_is_delivered(self) -> None:
        self._inject_rules([GITHUB_REPO], redact=[REDACT])
        event = _event(IncomingEvent.Source.GITHUB, channel_ref=GITHUB_REPO, thread_ref="17", key="gh:clean")
        host = MagicMock()
        host.post_pr_comment.return_value = {"html_url": "https://github.com/owner/pub-repo/pull/17#issuecomment-1"}

        dispatch = GitHubReplier(host=host).post_comment(
            event=event,
            target_ref=GITHUB_REPO,
            body="An ordinary review note with nothing sensitive.",
            idempotency_key="gh:clean:c",
        )

        assert dispatch.status == ReplyDispatch.Status.SENT
        host.post_pr_comment.assert_called_once_with(
            repo=GITHUB_REPO,
            pr_iid=17,
            body="An ordinary review note with nothing sensitive.",
        )

    def test_gitlab_mr_note_blocked_on_redact_term(self) -> None:
        self._inject_rules([GITLAB_REPO], redact=[REDACT])
        event = _event(IncomingEvent.Source.GITLAB, channel_ref=GITLAB_REPO, thread_ref="42", key="gl:leak")
        client = MagicMock()
        client.resolve_project.return_value = MagicMock(project_id=777)

        dispatch = GitLabReplier(client=client).post_comment(
            event=event,
            target_ref=GITLAB_REPO,
            body=f"MR note exposing {REDACT}.",
            idempotency_key="gl:leak:n",
        )

        assert dispatch.status == ReplyDispatch.Status.FAILED
        client.post_json.assert_not_called()

    def test_post_dm_is_never_privacy_scanned(self) -> None:
        # channel_ref is a public repo and the body carries a redact term, yet a
        # bot→user DM is excluded from the on-behalf actions, so it is not scanned.
        self._inject_rules([GITHUB_REPO], redact=[REDACT])
        event = _event(IncomingEvent.Source.SLACK, channel_ref=GITHUB_REPO, thread_ref="", key="dm:leak")
        bot = MagicMock()
        bot.open_dm.return_value = "D-USER"

        dispatch = SlackReplier(bot=bot).post_dm(
            event=event,
            actor="U_ALICE",
            body=f"FYI the run touched {REDACT}.",
            idempotency_key="dm:leak:d",
        )

        assert dispatch.status == ReplyDispatch.Status.SENT
        bot.post_message.assert_called_once()

    def test_clean_slack_thread_reply_is_not_blocked(self) -> None:
        # A Slack channel ref is never in public_repos, so a thread reply passes
        # even when redact terms are configured.
        self._inject_rules([GITHUB_REPO], redact=[REDACT])
        event = _event(IncomingEvent.Source.SLACK, channel_ref="C-eng", thread_ref="170.1", key="slack:reply")
        bot = MagicMock()

        dispatch = SlackReplier(bot=bot).post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="170.1",
            body=f"discussing {REDACT} freely on our private channel",
            idempotency_key="slack:reply:r",
        )

        assert dispatch.status == ReplyDispatch.Status.SENT
        bot.post_message.assert_called_once()

    def test_redeliver_blocked_on_privacy_finding(self) -> None:
        self._inject_rules([GITHUB_REPO], redact=[REDACT])
        event = _event(IncomingEvent.Source.GITHUB, channel_ref=GITHUB_REPO, thread_ref="17", key="gh:rd")
        dispatch = ReplyDispatch.objects.create(
            event=event,
            target_ref=GITHUB_REPO,
            action_name="post_comment",
            idempotency_key="gh:rd:c",
            status=ReplyDispatch.Status.FAILED,
            body=f"retrying a body that leaks {REDACT}.",
        )
        host = MagicMock()

        with pytest.raises(PublicationPrivacyBlockedError):
            GitHubReplier(host=host).redeliver(dispatch)

        host.post_pr_comment.assert_not_called()
