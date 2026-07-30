"""Tests for :class:`SlackBroadcastsScanner` — channel-poll broadcast loop (#1131).

The scanner polls one or more configured Slack channels, extracts MR URLs
from each message, classifies the set via an injected classifier, and:

* reacts ``:white_check_mark:`` and skips dispatch when every MR is merged +
    approved;
* reacts ``:eyes:`` and emits one ``slack.review_intent`` signal per open
    MR in the open subset for mixed and all-pending broadcasts;
* persists one :class:`ScannedBroadcast` row per ``(channel, slack_ts)``
    for idempotent re-scans;
* hard-fails with :class:`ConnectChannelBotRestrictedError` on Slack-Connect
    bot-restricted channels until the dual-token write path (#1209) lands.
"""

import subprocess
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from django.db import OperationalError
from django.test import TestCase

from teatree.core.models import ScannedBroadcast, Session, Task, Ticket
from teatree.loop.scanners.slack_broadcast_mr_classifier import GlabGhMrStateClassifier
from teatree.loop.scanners.slack_broadcasts import ConnectChannelBotRestrictedError, MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate


@pytest.fixture(autouse=True)
def _gate_off(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    disable_on_behalf_gate(tmp_path_factory, monkeypatch)


@pytest.fixture(autouse=True)
def _repo_public_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # #1773: the trusted-set author classification treats the broadcast repos as
    # PUBLIC so a colleague (non-trusted) author is correctly untrusted — no live
    # ``glab`` visibility probe in the test path. An own/trusted author is still
    # excluded via the trusted-set check.
    monkeypatch.setattr("teatree.core.review.author_trust.repo_is_internal", lambda *a, **k: False)


CHANNEL = "C0DEMOCHAN1"
TS_A = "1779201478.501469"
TS_B = "1779201499.123456"
MR_MERGED = "https://gitlab.example.com/team/project/-/merge_requests/6044"
MR_MERGED_2 = "https://gitlab.example.com/team/project/-/merge_requests/6224"
MR_OPEN = "https://gitlab.example.com/team/project/-/merge_requests/7432"
MR_OPEN_2 = "https://gitlab.example.com/team/project/-/merge_requests/7438"


@dataclass
class FakeMessaging:
    """Minimal MessagingBackend stub recording react calls."""

    user_id: str = "U0DEMOUSER1"
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)
    react_raises: BaseException | None = None

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        if self.react_raises is not None:
            raise self.react_raises
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        return self.react(channel=channel, ts=ts, emoji=emoji)

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = (channel, ts)
        return {}

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = (channel, text, thread_ts)
        return {}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        _ = (channel, ts, text)
        return {}

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return ""

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example/{channel}/p{ts.replace('.', '')}"

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""

    def auth_test(self) -> RawAPIDict:
        return {"ok": True}


def _fetcher(messages_by_channel: dict[str, list[RawAPIDict]]):
    def fetch(*, channel: str) -> list[RawAPIDict]:
        return list(messages_by_channel.get(channel, []))

    return fetch


def _classifier(states: dict[str, MrState]):
    def classify(urls):
        return [states[url] for url in urls]

    return classify


def _message(text: str, ts: str) -> RawAPIDict:
    return {"text": text, "ts": ts, "user": "USRG", "type": "message"}


def _message_with_reactions(text: str, ts: str, reactions: list[RawAPIDict]) -> RawAPIDict:
    message = _message(text, ts)
    message["reactions"] = reactions
    return message


USER_SLACK_ID = "U0DEMOUSER1"
COLLEAGUE_SLACK_ID = "UC0LLEAGUE"


class TestClassificationBehaviour(TestCase):
    def test_all_merged_broadcast_reacts_green_check_and_skips_dispatch(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"please review {MR_MERGED} and {MR_MERGED_2}", TS_A)]}
        states = {
            MR_MERGED: MrState(url=MR_MERGED, merged=True, approved=True),
            MR_MERGED_2: MrState(url=MR_MERGED_2, merged=True, approved=True),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )

        signals = scanner.scan()

        assert signals == []
        assert backend.react_calls == [(CHANNEL, TS_A, "white_check_mark")]
        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS_A)
        assert row.classification == ScannedBroadcast.Classification.ALL_MERGED

    def test_all_pending_broadcast_dispatches_every_url_without_eyes_claim(self) -> None:
        # #113/#86: an open colleague MR queues a reviewer dispatch but posts
        # NO ``:eyes:`` claim reaction at discovery — the claim reaction
        # belongs to review-DONE, never to start.
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"new review thread {MR_OPEN} {MR_OPEN_2}", TS_A)]}
        states = {
            MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False),
            MR_OPEN_2: MrState(url=MR_OPEN_2, merged=False, approved=False),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["slack.review_intent", "slack.review_intent"]
        assert {s.payload["mr_url"] for s in signals} == {MR_OPEN, MR_OPEN_2}
        assert {s.payload["trigger"] for s in signals} == {"broadcast"}
        assert backend.react_calls == []
        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS_A)
        assert row.classification == ScannedBroadcast.Classification.PENDING
        assert row.mr_urls == [MR_OPEN, MR_OPEN_2]

    def test_mixed_broadcast_dispatches_only_open_subset_without_eyes_claim(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"{MR_MERGED} {MR_OPEN}", TS_A)]}
        states = {
            MR_MERGED: MrState(url=MR_MERGED, merged=True, approved=True),
            MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )

        signals = scanner.scan()

        assert len(signals) == 1
        assert signals[0].payload["mr_url"] == MR_OPEN
        assert backend.react_calls == []


class TestSkipsEyesOnOwnMrBroadcasts(TestCase):
    """Own-author MR broadcasts must not get the ``:eyes:`` review reaction (#1384).

    The ``:eyes:`` reaction signals "I'm looking at this colleague's MR". On a
    broadcast whose every open MR is authored by the current user it is
    meaningless noise the user has to remove by hand. The scanner skips both
    the reaction and the reviewer-dispatch signals when ``current_gitlab_username``
    matches the author of every open MR in the broadcast (sibling of #1321's
    review-sweep own-author exclusion). BINDING
    ``feedback_no_eyes_react_on_own_mr_broadcasts``.
    """

    def test_sole_own_mr_broadcast_skips_eyes_and_dispatch(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"please review {MR_OPEN}", TS_A)]}
        states = {MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False, author_username="me")}
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_gitlab_username="me",
        )

        signals = scanner.scan()

        assert signals == []
        assert backend.react_calls == []
        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS_A)
        assert row.classification == ScannedBroadcast.Classification.PENDING

    def test_colleague_mr_broadcast_dispatches_without_eyes_claim(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"please review {MR_OPEN}", TS_A)]}
        states = {MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False, author_username="colleague")}
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_gitlab_username="me",
        )

        signals = scanner.scan()

        assert [s.payload["mr_url"] for s in signals] == [MR_OPEN]
        # #113/#86: a colleague MR is dispatched but NOT :eyes:-claimed at
        # discovery — the claim reaction is review-DONE-only.
        assert backend.react_calls == []

    def test_mixed_authorship_open_subset_dispatches_without_eyes_claim(self) -> None:
        # One own MR + one colleague MR open in the same broadcast: a
        # colleague MR still needs review, so the broadcast is not "all mine"
        # and the dispatch for the colleague's MR must fire — with no :eyes:
        # claim reaction at discovery (#113/#86).
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"{MR_OPEN} {MR_OPEN_2}", TS_A)]}
        states = {
            MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False, author_username="me"),
            MR_OPEN_2: MrState(url=MR_OPEN_2, merged=False, approved=False, author_username="colleague"),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_gitlab_username="me",
        )

        signals = scanner.scan()

        assert {s.payload["mr_url"] for s in signals} == {MR_OPEN, MR_OPEN_2}
        assert backend.react_calls == []


class TestTrustedSetAndAdversarial(TestCase):
    """#1773: own-author exclusion is a trusted-SET check; untrusted public author is adversarial."""

    def test_trusted_set_author_other_than_current_username_skips_eyes(self) -> None:
        # An MR authored by a seeded trusted identity (not the configured
        # ``current_gitlab_username``) is still the user's own work → skip eyes.
        from teatree.core.models import TrustedIdentity  # noqa: PLC0415

        TrustedIdentity.objects.get_or_create(platform="gitlab", handle="adrien.cossa")
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"please review {MR_OPEN}", TS_A)]}
        states = {MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False, author_username="adrien.cossa")}
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_gitlab_username="me",
        )

        signals = scanner.scan()

        assert signals == []
        assert backend.react_calls == []

    def test_untrusted_public_author_signal_flags_adversarial(self) -> None:
        from teatree.core.models import TrustedIdentity  # noqa: PLC0415

        TrustedIdentity.objects.get_or_create(platform="gitlab", handle="adrien.cossa")
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"please review {MR_OPEN}", TS_A)]}
        states = {MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False, author_username="evilhacker")}
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_gitlab_username="me",
        )

        signals = scanner.scan()

        assert [s.payload["mr_url"] for s in signals] == [MR_OPEN]
        assert signals[0].payload["adversarial"] is True
        assert signals[0].payload["requires_human_authorization"] is True


class TestSkipsBroadcastsAlreadyEyesReactedByColleague(TestCase):
    """A broadcast already :eyes:-reacted by a colleague must not be dispatched.

    Standing user rule: never dispatch a review of an MR/PR carrying a
    :eyes: reaction from someone other than the user — that reaction is the
    colleague's claim on the review. The override is an explicit
    ``<@user_slack_id>`` mention: the user naming the MR re-opens dispatch.
    """

    def _scanner(self, history: dict[str, list[RawAPIDict]]) -> SlackBroadcastsScanner:
        return SlackBroadcastsScanner(
            backend=FakeMessaging(user_id=USER_SLACK_ID),
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier({MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False)}),
        )

    def test_colleague_eyes_reaction_skips_react_and_dispatch(self) -> None:
        message = _message_with_reactions(
            f"please review {MR_OPEN}",
            TS_A,
            [{"name": "eyes", "users": [COLLEAGUE_SLACK_ID], "count": 1}],
        )
        scanner = self._scanner({CHANNEL: [message]})

        signals = scanner.scan()

        assert signals == []
        assert scanner.backend.react_calls == []
        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS_A)
        assert row.classification == ScannedBroadcast.Classification.PENDING

    def test_own_eyes_reaction_does_not_count_as_a_colleague_claim(self) -> None:
        message = _message_with_reactions(
            f"please review {MR_OPEN}",
            TS_A,
            [{"name": "eyes", "users": [USER_SLACK_ID], "count": 1}],
        )
        scanner = self._scanner({CHANNEL: [message]})

        signals = scanner.scan()

        assert [s.payload["mr_url"] for s in signals] == [MR_OPEN]
        # No discovery-time claim reaction (#113/#86).
        assert scanner.backend.react_calls == []

    def test_non_eyes_colleague_reaction_still_dispatches(self) -> None:
        message = _message_with_reactions(
            f"please review {MR_OPEN}",
            TS_A,
            [{"name": "thumbsup", "users": [COLLEAGUE_SLACK_ID], "count": 1}],
        )
        scanner = self._scanner({CHANNEL: [message]})

        signals = scanner.scan()

        assert [s.payload["mr_url"] for s in signals] == [MR_OPEN]
        assert scanner.backend.react_calls == []

    def test_no_user_id_configured_cannot_be_overridden_by_mention(self) -> None:
        message = _message_with_reactions(
            f"<@{USER_SLACK_ID}> please review {MR_OPEN}",
            TS_A,
            [{"name": "eyes", "users": [COLLEAGUE_SLACK_ID], "count": 1}],
        )
        scanner = SlackBroadcastsScanner(
            backend=FakeMessaging(user_id=""),
            channels=[CHANNEL],
            fetch_channel_history=_fetcher({CHANNEL: [message]}),
            classify_mrs=_classifier({MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False)}),
        )

        signals = scanner.scan()

        assert signals == []
        assert scanner.backend.react_calls == []

    def test_user_mention_overrides_colleague_eyes_reaction(self) -> None:
        message = _message_with_reactions(
            f"<@{USER_SLACK_ID}> please review {MR_OPEN}",
            TS_A,
            [{"name": "eyes", "users": [COLLEAGUE_SLACK_ID], "count": 1}],
        )
        scanner = SlackBroadcastsScanner(
            backend=FakeMessaging(user_id=USER_SLACK_ID),
            channels=[CHANNEL],
            fetch_channel_history=_fetcher({CHANNEL: [message]}),
            classify_mrs=_classifier({MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False)}),
            user_slack_id=USER_SLACK_ID,
            reviewer_username="me",
        )

        signals = scanner.scan()

        assert {s.kind for s in signals} == {"slack.review_intent", "review_request_in_slack"}
        assert {s.payload["mr_url"] for s in signals} == {MR_OPEN}
        # The user mention re-opens dispatch, but still no discovery-time
        # claim reaction (#113/#86).
        assert scanner.backend.react_calls == []


class TestIdempotency(TestCase):
    def _pending_scanner(self, backend: FakeMessaging) -> SlackBroadcastsScanner:
        return SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher({CHANNEL: [_message(f"{MR_OPEN}", TS_A)]}),
            classify_mrs=_classifier({MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False)}),
        )

    def test_rescan_reuses_the_single_ledger_row(self) -> None:
        backend = FakeMessaging()
        scanner = self._pending_scanner(backend)

        scanner.scan()
        scanner.scan()

        # No discovery-time claim reaction on either scan (#113/#86).
        assert backend.react_calls == []
        assert ScannedBroadcast.objects.filter(channel=CHANNEL, slack_ts=TS_A).count() == 1

    def test_rescan_re_emits_while_no_reviewer_task_covers_the_row(self) -> None:
        """The ledger is the dedup key, not the emission gate.

        A dispatch lost before a reviewer task existed — dead worker, exhausted
        budget, a stopped review loop — would otherwise make this review
        permanently unreachable. Duplicate work is prevented downstream, where
        ``persist_agent_actions`` reuses the open reviewing Task.
        """
        scanner = self._pending_scanner(FakeMessaging())

        first = scanner.scan()
        second = scanner.scan()

        assert len(first) == 1
        assert [signal.payload["mr_url"] for signal in second] == [MR_OPEN]

    def test_rescan_stops_emitting_once_a_reviewer_task_covers_the_row(self) -> None:
        scanner = self._pending_scanner(FakeMessaging())
        scanner.scan()
        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS_A)
        ticket = Ticket.objects.create(issue_url=MR_OPEN, role=Ticket.Role.REVIEWER)
        session = Session.objects.create(ticket=ticket, agent_id="t3:reviewer")
        row.attach_reviewer_task(str(Task.objects.create(ticket=ticket, session=session, phase="reviewing").pk))

        assert scanner.scan() == []

    def test_pending_to_all_merged_reclassifies_and_reacts_green(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"{MR_OPEN}", TS_A)]}
        pending_states = {MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False)}
        merged_states = {MR_OPEN: MrState(url=MR_OPEN, merged=True, approved=True)}

        pending_scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(pending_states),
        )
        pending_scanner.scan()

        merged_scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(merged_states),
        )
        signals = merged_scanner.scan()

        assert signals == []
        # No :eyes: claim on the pending scan (#113/#86); the all-merged
        # reclassification posts the :white_check_mark: outcome reaction.
        assert backend.react_calls == [(CHANNEL, TS_A, "white_check_mark")]
        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS_A)
        assert row.classification == ScannedBroadcast.Classification.ALL_MERGED
        assert row.reclassified_at is not None


class TestConnectChannelHardFail(TestCase):
    def test_connect_channel_bot_restricted_hard_fails(self) -> None:
        # The only reaction the scanner posts now is the all-merged
        # :white_check_mark: outcome reaction; a Connect-restricted channel
        # rejecting it must still hard-fail loudly (#1131).
        backend = FakeMessaging(react_raises=RuntimeError("Slack API not_in_channel"))
        history = {CHANNEL: [_message(f"{MR_MERGED}", TS_A)]}
        states = {MR_MERGED: MrState(url=MR_MERGED, merged=True, approved=True)}
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )

        with pytest.raises(ConnectChannelBotRestrictedError) as exc_info:
            scanner.scan()

        assert exc_info.value.channel == CHANNEL


class TestNoiseHandling(TestCase):
    def test_messages_without_mr_urls_are_ignored(self) -> None:
        backend = FakeMessaging()
        history = {
            CHANNEL: [
                _message("good morning team", TS_A),
                _message(f"and another {MR_OPEN}", TS_B),
            ],
        }
        states = {MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False)}
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )

        signals = scanner.scan()

        assert len(signals) == 1
        assert signals[0].payload["mr_url"] == MR_OPEN
        assert backend.react_calls == []
        assert ScannedBroadcast.objects.count() == 1


class TestGlabGhMrStateClassifierUsesRepoFlag:
    """``GlabGhMrStateClassifier`` calls ``glab mr view -R <project> <iid>``.

    Outside a repo cwd ``glab mr view <full-url>`` silently early-exits
    because glab refuses to resolve the host from a URL alone — the
    scanner process has no git remote to anchor against. With
    ``-R <project>`` plus the numeric IID glab routes the API call
    directly. This pin makes a bare-URL invocation a hard test failure.
    """

    def test_gitlab_classifier_invokes_repo_flagged_form(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        url = "https://gitlab.example.com/team/project/-/merge_requests/7446"
        captured: list[list[str]] = []

        def fake_run(cmd, *, expected_codes=None, env=None):
            captured.append(list(cmd))
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"state": "merged", "upvotes": 2}',
                stderr="",
            )

        monkeypatch.setattr("teatree.utils.run.run_allowed_to_fail", fake_run)
        monkeypatch.setattr("shutil.which", lambda _arg: "/usr/bin/glab")

        classifier = GlabGhMrStateClassifier(glab_token="glpat-fake")
        states = classifier([url])

        assert len(states) == 1
        assert states[0].merged is True
        assert states[0].approved is True
        assert len(captured) == 1
        cmd = captured[0]
        # The repo-flagged form: ``glab mr view -R <project> <iid> -F json``.
        # ``-R`` is what tells glab the project to query against; passing
        # the full URL as the positional arg is the buggy form being pinned out.
        assert "-R" in cmd
        r_idx = cmd.index("-R")
        assert cmd[r_idx + 1] == "team/project"
        assert "7446" in cmd
        assert url not in cmd  # the full URL must NOT be passed as a positional


class TestClassifierReadsAuthorUsername:
    """``GlabGhMrStateClassifier`` surfaces the MR author username (#1384).

    The own-MR ``:eyes:`` skip needs the author identity. GitLab JSON
    carries it under ``author.username``; GitHub under ``author.login``.
    A missing/malformed author block degrades to an empty username, which
    the scanner treats as "not mine".
    """

    def test_gitlab_reads_author_username(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = "https://gitlab.example.com/team/project/-/merge_requests/7446"

        def fake_run(cmd, *, expected_codes=None, env=None):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"state": "opened", "upvotes": 0, "author": {"username": "me"}}',
                stderr="",
            )

        monkeypatch.setattr("teatree.utils.run.run_allowed_to_fail", fake_run)
        monkeypatch.setattr("shutil.which", lambda _arg: "/usr/bin/glab")

        states = GlabGhMrStateClassifier(glab_token="glpat-fake")([url])

        assert states[0].author_username == "me"
        assert states[0].merged is False

    def test_gitlab_missing_author_block_yields_empty_username(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = "https://gitlab.example.com/team/project/-/merge_requests/7446"

        def fake_run(cmd, *, expected_codes=None, env=None):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"state": "opened", "upvotes": 0}',
                stderr="",
            )

        monkeypatch.setattr("teatree.utils.run.run_allowed_to_fail", fake_run)
        monkeypatch.setattr("shutil.which", lambda _arg: "/usr/bin/glab")

        states = GlabGhMrStateClassifier(glab_token="glpat-fake")([url])

        assert states[0].author_username == ""

    def test_github_reads_author_login(self, monkeypatch: pytest.MonkeyPatch) -> None:
        url = "https://github.com/owner/repo/pull/42"
        captured: list[list[str]] = []

        def fake_run(cmd, *, expected_codes=None, env=None):
            captured.append(list(cmd))
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"state": "OPEN", "reviewDecision": "", "author": {"login": "me"}}',
                stderr="",
            )

        monkeypatch.setattr("teatree.utils.run.run_allowed_to_fail", fake_run)
        monkeypatch.setattr("shutil.which", lambda _arg: "/usr/bin/gh")

        states = GlabGhMrStateClassifier(github_token="ghp-fake")([url])

        assert states[0].author_username == "me"
        # The author field must be in the requested json columns.
        assert "author" in captured[0][captured[0].index("--json") + 1].split(",")


class TestScannerSkipsOnMissingMigration(TestCase):
    """Scanner skips gracefully when core migration 0028 hasn't been run (#1260).

    Without the ``teatree_scanned_broadcast`` table the first
    ``ScannedBroadcast.record`` raises a missing-relation error
    (sqlite ``OperationalError``, Postgres ``ProgrammingError``).
    Sibling pattern lives in :class:`IncomingEventsScanner` — both
    detect that class and degrade to a quiet info log so the rest of
    the scanner registry keeps running.
    """

    def test_scanner_returns_empty_when_scannedbroadcast_table_missing(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"{MR_OPEN}", TS_A)]}
        states = {MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False)}
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )

        with patch.object(
            ScannedBroadcast,
            "record",
            side_effect=OperationalError("no such table: teatree_scanned_broadcast"),
        ):
            signals = scanner.scan()

        # Graceful skip: no signals, no crash, no react attempt (DB write
        # fails before any side effect lands).
        assert signals == []
        assert backend.react_calls == []
