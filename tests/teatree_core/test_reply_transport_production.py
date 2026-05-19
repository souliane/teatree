"""Behaviour tests for the production Slack/GitLab/GitHub repliers (#668).

The on-behalf gate (#960) is exercised by its own dedicated suite in
``test_reply_transport_on_behalf_gate.py``; these tests exercise
production-replier wiring (backend dispatch, error handling, source
routing) and disable the gate so the assertions on backend behaviour
still hold.
"""

from unittest.mock import MagicMock

import pytest
from django.test import TestCase

from teatree.core.models import IncomingEvent, ReplyDispatch
from teatree.core.reply_transport import GitHubReplier, GitLabReplier, NoopReplier, ReplySpec, SlackReplier, replier_for
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate


@pytest.fixture(autouse=True)
def _no_on_behalf_gate(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    disable_on_behalf_gate(tmp_path_factory, monkeypatch)


def _event(source: str, *, key: str, **fields: object) -> IncomingEvent:
    base = {
        "source": source,
        "actor": "U_ALICE",
        "channel_ref": "C-eng",
        "thread_ref": "1700000000.0001",
        "body": "hi",
        "idempotency_key": key,
    }
    base.update(fields)
    return IncomingEvent.objects.create(**base)


class TestSlackReplier(TestCase):
    def test_post_in_thread_calls_backend_and_records_sent(self) -> None:
        event = _event(IncomingEvent.Source.SLACK, key="slack:1")
        bot = MagicMock()
        replier = SlackReplier(bot=bot)

        dispatch = replier.post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="1700000000.0001",
            body="hello thread",
            idempotency_key="slack:1:reply",
        )

        bot.post_message.assert_called_once_with(
            channel="C-eng",
            text="hello thread",
            thread_ts="1700000000.0001",
        )
        assert dispatch.status == ReplyDispatch.Status.SENT

    def test_post_dm_opens_dm_then_posts(self) -> None:
        event = _event(IncomingEvent.Source.SLACK, key="slack:2")
        bot = MagicMock()
        bot.open_dm.return_value = "D-CHAN"
        replier = SlackReplier(bot=bot)

        dispatch = replier.post_dm(event=event, actor="U_ALICE", body="psst", idempotency_key="slack:2:dm")

        bot.open_dm.assert_called_once_with("U_ALICE")
        bot.post_message.assert_called_once_with(channel="D-CHAN", text="psst", thread_ts="")
        assert dispatch.status == ReplyDispatch.Status.SENT

    def test_post_dm_targets_passed_actor_not_event_actor(self) -> None:
        # event.actor is U_ALICE; the caller DMs a different user (U_LEAD).
        event = _event(IncomingEvent.Source.SLACK, key="slack:2b", actor="U_ALICE")
        bot = MagicMock()
        bot.open_dm.return_value = "D-LEAD"
        replier = SlackReplier(bot=bot)

        replier.post_dm(event=event, actor="U_LEAD", body="heads up", idempotency_key="slack:2b:dm")

        bot.open_dm.assert_called_once_with("U_LEAD")

    def test_post_dm_failed_when_open_dm_returns_empty(self) -> None:
        event = _event(IncomingEvent.Source.SLACK, key="slack:2c")
        bot = MagicMock()
        bot.open_dm.return_value = ""
        replier = SlackReplier(bot=bot)

        dispatch = replier.post_dm(event=event, actor="U_GHOST", body="x", idempotency_key="slack:2c:dm")

        assert dispatch.status == ReplyDispatch.Status.FAILED
        assert "could not open DM" in dispatch.error_message
        bot.post_message.assert_not_called()

    def test_backend_failure_records_failed_with_error(self) -> None:
        event = _event(IncomingEvent.Source.SLACK, key="slack:3")
        bot = MagicMock()
        bot.post_message.side_effect = RuntimeError("slack 500")
        replier = SlackReplier(bot=bot)

        dispatch = replier.post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="t",
            body="x",
            idempotency_key="slack:3:reply",
        )

        assert dispatch.status == ReplyDispatch.Status.FAILED
        assert "slack 500" in dispatch.error_message

    def test_idempotent_replay_does_not_repost(self) -> None:
        event = _event(IncomingEvent.Source.SLACK, key="slack:4")
        bot = MagicMock()
        replier = SlackReplier(bot=bot)

        first = replier.post_in_thread(
            event=event, target_ref="C", thread_ref="t", body="x", idempotency_key="slack:4:r"
        )
        second = replier.post_in_thread(
            event=event, target_ref="C", thread_ref="t", body="x", idempotency_key="slack:4:r"
        )

        assert first.pk == second.pk
        bot.post_message.assert_called_once()


class TestGitLabReplier(TestCase):
    def test_post_comment_resolves_project_and_posts_note(self) -> None:
        event = _event(
            IncomingEvent.Source.GITLAB,
            key="gitlab:1",
            channel_ref="org/repo",
            thread_ref="42",
        )
        client = MagicMock()
        client.resolve_project.return_value = MagicMock(project_id=777)
        replier = GitLabReplier(client=client)

        dispatch = replier.post_comment(
            event=event,
            target_ref="org/repo",
            body="LGTM",
            idempotency_key="gitlab:1:note",
        )

        client.resolve_project.assert_called_once_with("org/repo")
        client.post_json.assert_called_once_with(
            "projects/777/merge_requests/42/notes",
            {"body": "LGTM"},
        )
        assert dispatch.status == ReplyDispatch.Status.SENT

    def test_unresolvable_project_records_failed(self) -> None:
        event = _event(
            IncomingEvent.Source.GITLAB,
            key="gitlab:2",
            channel_ref="bad/repo",
            thread_ref="9",
        )
        client = MagicMock()
        client.resolve_project.return_value = None
        replier = GitLabReplier(client=client)

        dispatch = replier.post_comment(event=event, target_ref="bad/repo", body="x", idempotency_key="gitlab:2:n")

        assert dispatch.status == ReplyDispatch.Status.FAILED
        client.post_json.assert_not_called()


class TestDeliverPostedRefForAfterReceipt(TestCase):
    """``_deliver`` returns the posted artifact ref (#949) — the after-receipt link.

    These exercise the narrow return value each subclass derives so the
    ``_send`` after-receipt DM has a clickable URL. The Slack helper is
    known to raise; the GitLab/GitHub helpers fall back to the canonical
    MR/PR ref when the API response carries no URL.
    """

    def _slack_spec(self, *, channel: str = "C-eng") -> ReplySpec:
        event = _event(IncomingEvent.Source.SLACK, key=f"d:slack:{channel}", channel_ref=channel)
        return ReplySpec(
            event=event,
            target_ref=channel,
            body="hi",
            idempotency_key=f"d:slack:{channel}:k",
            action_name="post_comment",
        )

    def test_slack_deliver_returns_permalink_on_success(self) -> None:
        bot = MagicMock()
        bot.post_message.return_value = {"ok": True, "ts": "1700000000.0001"}
        bot.get_permalink.return_value = "https://team.slack.com/archives/C-eng/p1"
        ref = SlackReplier(bot=bot)._deliver(self._slack_spec())
        assert ref == "https://team.slack.com/archives/C-eng/p1"

    def test_slack_deliver_falls_back_to_channel_link_when_no_ts(self) -> None:
        bot = MagicMock()
        bot.post_message.return_value = {"ok": True}  # no ts
        ref = SlackReplier(bot=bot)._deliver(self._slack_spec(channel="C-x"))
        assert ref == "slack://channel?id=C-x"

    def test_slack_deliver_falls_back_when_get_permalink_raises(self) -> None:
        bot = MagicMock()
        bot.post_message.return_value = {"ok": True, "ts": "1.2"}
        bot.get_permalink.side_effect = RuntimeError("slack permalink boom")
        ref = SlackReplier(bot=bot)._deliver(self._slack_spec(channel="C-z"))
        assert ref == "slack://channel?id=C-z"

    def test_slack_deliver_post_dm_returns_empty(self) -> None:
        bot = MagicMock()
        bot.open_dm.return_value = "D1"
        event = _event(IncomingEvent.Source.SLACK, key="d:slack:dm")
        spec = ReplySpec(event=event, target_ref="U1", body="hi", idempotency_key="d:slack:dm:k", action_name="post_dm")
        assert SlackReplier(bot=bot)._deliver(spec) == ""

    def _gitlab_replier(self, post_json_return: object) -> tuple[GitLabReplier, ReplySpec]:
        client = MagicMock()
        client.resolve_project.return_value = MagicMock(project_id=777)
        client.post_json.return_value = post_json_return
        event = _event(IncomingEvent.Source.GITLAB, key="d:gl", channel_ref="org/repo", thread_ref="42")
        spec = ReplySpec(
            event=event, target_ref="org/repo", body="x", idempotency_key="d:gl:k", action_name="post_comment"
        )
        return GitLabReplier(client=client), spec

    def test_gitlab_deliver_returns_note_web_url(self) -> None:
        replier, spec = self._gitlab_replier({"id": 1, "web_url": "https://gl/x/-/mr/42#note_1"})
        assert replier._deliver(spec) == "https://gl/x/-/mr/42#note_1"

    def test_gitlab_deliver_falls_back_to_canonical_mr_ref(self) -> None:
        replier, spec = self._gitlab_replier({"id": 1})  # no web_url/html_url
        assert replier._deliver(spec) == "org/repo!42"

    def test_gitlab_deliver_falls_back_when_response_not_dict(self) -> None:
        replier, spec = self._gitlab_replier(None)
        assert replier._deliver(spec) == "org/repo!42"

    def _github_replier(self, comment_return: object) -> tuple[GitHubReplier, ReplySpec]:
        host = MagicMock()
        host.post_pr_comment.return_value = comment_return
        event = _event(IncomingEvent.Source.GITHUB, key="d:gh", channel_ref="owner/repo", thread_ref="17")
        spec = ReplySpec(
            event=event, target_ref="owner/repo", body="x", idempotency_key="d:gh:k", action_name="post_comment"
        )
        return GitHubReplier(host=host), spec

    def test_github_deliver_returns_comment_html_url(self) -> None:
        replier, spec = self._github_replier({"html_url": "https://github.com/owner/repo/pull/17#issuecomment-9"})
        assert replier._deliver(spec) == "https://github.com/owner/repo/pull/17#issuecomment-9"

    def test_github_deliver_falls_back_to_canonical_pr_ref(self) -> None:
        replier, spec = self._github_replier({})  # no html_url/web_url
        assert replier._deliver(spec) == "owner/repo#17"


class TestGitHubReplier(TestCase):
    def test_post_comment_calls_backend(self) -> None:
        event = _event(
            IncomingEvent.Source.GITHUB,
            key="github:1",
            channel_ref="owner/repo",
            thread_ref="17",
        )
        host = MagicMock()
        replier = GitHubReplier(host=host)

        dispatch = replier.post_comment(
            event=event,
            target_ref="owner/repo",
            body="ship it",
            idempotency_key="github:1:c",
        )

        host.post_pr_comment.assert_called_once_with(repo="owner/repo", pr_iid=17, body="ship it")
        assert dispatch.status == ReplyDispatch.Status.SENT

    def test_non_numeric_pr_number_records_failed(self) -> None:
        event = _event(
            IncomingEvent.Source.GITHUB,
            key="github:2",
            channel_ref="owner/repo",
            thread_ref="not-a-pr",
        )
        host = MagicMock()
        replier = GitHubReplier(host=host)

        dispatch = replier.post_comment(event=event, target_ref="owner/repo", body="x", idempotency_key="github:2:c")

        assert dispatch.status == ReplyDispatch.Status.FAILED
        host.post_pr_comment.assert_not_called()


class TestRecordRaceRecovery(TestCase):
    def test_integrity_error_recovers_existing_row(self) -> None:
        # Simulate a row created between _send's SELECT and _record's
        # INSERT by writing the ReplyDispatch from inside _deliver.
        event = _event(IncomingEvent.Source.SLACK, key="slack:race")

        class RacingReplier(NoopReplier):
            def _deliver(self, spec: ReplySpec) -> str:
                ReplyDispatch.objects.create(
                    event=spec.event,
                    target_ref=spec.target_ref,
                    action_name=spec.action_name,
                    idempotency_key=spec.idempotency_key,
                    status=ReplyDispatch.Status.SENT,
                )
                return ""

        dispatch = RacingReplier().post_dm(event=event, actor="U", body="x", idempotency_key="slack:race:d")

        assert dispatch.idempotency_key == "slack:race:d"
        assert ReplyDispatch.objects.filter(idempotency_key="slack:race:d").count() == 1


class TestIntrinsicIdempotency(TestCase):
    def test_concurrent_caller_with_same_key_does_not_double_deliver(self) -> None:
        """The idempotency guarantee is intrinsic to the key reservation.

        Not dependent on an external flock: a second caller racing on the
        same key reuses the reserved row and never calls ``_deliver``
        twice.
        """
        event = _event(IncomingEvent.Source.SLACK, key="slack:intrinsic")
        deliveries: list[str] = []

        class CountingReplier(NoopReplier):
            def _deliver(self, spec: ReplySpec) -> str:
                deliveries.append(spec.idempotency_key)
                return ""

        replier = CountingReplier()
        first = replier.post_dm(event=event, actor="U", body="x", idempotency_key="slack:intr:d")
        second = replier.post_dm(event=event, actor="U", body="x", idempotency_key="slack:intr:d")

        assert first.pk == second.pk
        assert deliveries == ["slack:intr:d"]
        assert ReplyDispatch.objects.filter(idempotency_key="slack:intr:d").count() == 1

    def test_failed_delivery_still_recorded_once(self) -> None:
        event = _event(IncomingEvent.Source.SLACK, key="slack:intrinsic-fail")
        bot = MagicMock()
        bot.open_dm.return_value = "D-1"
        bot.post_message.side_effect = RuntimeError("backend down")
        replier = SlackReplier(bot=bot)

        dispatch = replier.post_dm(event=event, actor="U", body="x", idempotency_key="slack:intr:f")

        assert dispatch.status == ReplyDispatch.Status.FAILED
        assert "backend down" in dispatch.error_message
        assert ReplyDispatch.objects.filter(idempotency_key="slack:intr:f").count() == 1


class TestReplierFactory(TestCase):
    def test_returns_slack_replier_for_slack_source(self) -> None:
        assert isinstance(replier_for(IncomingEvent.Source.SLACK, bot=MagicMock()), SlackReplier)

    def test_returns_gitlab_replier_for_gitlab_source(self) -> None:
        assert isinstance(replier_for(IncomingEvent.Source.GITLAB, gitlab=MagicMock()), GitLabReplier)

    def test_returns_github_replier_for_github_source(self) -> None:
        assert isinstance(replier_for(IncomingEvent.Source.GITHUB, github=MagicMock()), GitHubReplier)

    def test_falls_back_to_noop_when_backend_missing(self) -> None:
        assert isinstance(replier_for(IncomingEvent.Source.SLACK), NoopReplier)
        assert isinstance(replier_for(IncomingEvent.Source.CI), NoopReplier)

    def test_noop_still_records_sent(self) -> None:
        event = _event(IncomingEvent.Source.CI, key="ci:1")
        replier = replier_for(IncomingEvent.Source.CI)
        dispatch = replier.post_dm(event=event, actor="x", body="y", idempotency_key="ci:1:d")
        assert dispatch.status == ReplyDispatch.Status.SENT


class TestSharedReplierContract(TestCase):
    def test_existing_noop_behaviour_preserved(self) -> None:
        event = _event(IncomingEvent.Source.SLACK, key="slack:noop")
        dispatch = NoopReplier().post_in_thread(
            event=event,
            target_ref="C-eng",
            thread_ref="t1",
            body="hello",
            idempotency_key="slack:noop:r",
        )
        assert dispatch.status == ReplyDispatch.Status.SENT
        assert dispatch.target_ref == "C-eng/t1"

    def test_post_comment_requires_numeric_thread_ref_for_gitlab(self) -> None:
        event = _event(
            IncomingEvent.Source.GITLAB,
            key="gitlab:bad-iid",
            channel_ref="org/repo",
            thread_ref="not-a-number",
        )
        client = MagicMock()
        client.resolve_project.return_value = MagicMock(project_id=1)
        replier = GitLabReplier(client=client)

        dispatch = replier.post_comment(event=event, target_ref="org/repo", body="x", idempotency_key="gitlab:bad:n")

        assert dispatch.status == ReplyDispatch.Status.FAILED
        client.post_json.assert_not_called()
