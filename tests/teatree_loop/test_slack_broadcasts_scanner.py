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

from teatree.core.models import ScannedBroadcast
from teatree.loop.scanners.slack_broadcasts import (
    ConnectChannelBotRestrictedError,
    GlabGhMrStateClassifier,
    MrState,
    SlackBroadcastsScanner,
    _parse_gitlab_mr_url,
)
from teatree.types import RawAPIDict

CHANNEL = "C0AM3TENTLK"
TS_A = "1779201478.501469"
TS_B = "1779201499.123456"
MR_MERGED = "https://gitlab.example.com/team/project/-/merge_requests/6044"
MR_MERGED_2 = "https://gitlab.example.com/team/project/-/merge_requests/6224"
MR_OPEN = "https://gitlab.example.com/team/project/-/merge_requests/7432"
MR_OPEN_2 = "https://gitlab.example.com/team/project/-/merge_requests/7438"


@dataclass
class FakeMessaging:
    """Minimal MessagingBackend stub recording react calls."""

    user_id: str = "U0A72P7CK0A"
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)
    react_raises: BaseException | None = None

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        if self.react_raises is not None:
            raise self.react_raises
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

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

    def test_all_pending_broadcast_reacts_eyes_and_dispatches_every_url(self) -> None:
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
        assert backend.react_calls == [(CHANNEL, TS_A, "eyes")]
        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS_A)
        assert row.classification == ScannedBroadcast.Classification.PENDING
        assert row.mr_urls == [MR_OPEN, MR_OPEN_2]

    def test_mixed_broadcast_dispatches_only_open_subset(self) -> None:
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
        assert backend.react_calls == [(CHANNEL, TS_A, "eyes")]


class TestIdempotency(TestCase):
    def test_idempotent_rescan_is_a_noop(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"{MR_OPEN}", TS_A)]}
        states = {MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False)}
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )

        first = scanner.scan()
        second = scanner.scan()

        assert len(first) == 1
        assert second == []
        # Only one react across the two scans — the second scan no-ops on the
        # idempotency row.
        assert backend.react_calls == [(CHANNEL, TS_A, "eyes")]
        assert ScannedBroadcast.objects.filter(channel=CHANNEL, slack_ts=TS_A).count() == 1

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
        assert backend.react_calls == [
            (CHANNEL, TS_A, "eyes"),
            (CHANNEL, TS_A, "white_check_mark"),
        ]
        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS_A)
        assert row.classification == ScannedBroadcast.Classification.ALL_MERGED
        assert row.reclassified_at is not None


class TestConnectChannelHardFail(TestCase):
    def test_connect_channel_bot_restricted_hard_fails(self) -> None:
        backend = FakeMessaging(react_raises=RuntimeError("Slack API not_in_channel"))
        history = {CHANNEL: [_message(f"{MR_OPEN}", TS_A)]}
        states = {MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False)}
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
        assert backend.react_calls == [(CHANNEL, TS_B, "eyes")]
        assert ScannedBroadcast.objects.count() == 1


class TestParseGitlabMrUrl:
    """``_parse_gitlab_mr_url`` splits a GitLab MR URL into ``(project, iid)``.

    Drives the ``glab -R <project> <iid>`` form the classifier needs to
    work outside a repo cwd. The scanner runs from the loop process with
    no git remote, so ``glab mr view <url>`` (URL-only) silently exits
    non-zero and every broadcast is dropped — the fix is parsing the
    project path out of the URL and passing it to ``-R``.
    """

    def test_simple_group_and_project(self) -> None:
        assert _parse_gitlab_mr_url("https://gitlab.example.com/team/project/-/merge_requests/7446") == (
            "team/project",
            "7446",
        )

    def test_nested_subgroups(self) -> None:
        # GitLab allows arbitrary subgroup nesting; the project path
        # everything before ``/-/merge_requests/`` is the project.
        assert _parse_gitlab_mr_url("https://gitlab.example.com/team/sub/api/-/merge_requests/123") == (
            "team/sub/api",
            "123",
        )

    def test_trailing_slash(self) -> None:
        assert _parse_gitlab_mr_url("https://gitlab.example.com/team/project/-/merge_requests/1/") == (
            "team/project",
            "1",
        )

    def test_non_gitlab_url_returns_none(self) -> None:
        assert _parse_gitlab_mr_url("https://github.com/owner/repo/pull/42") is None

    def test_malformed_returns_none(self) -> None:
        assert _parse_gitlab_mr_url("not-a-url") is None


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


class TestSkipsEyesOnOwnMrBroadcasts(TestCase):
    """Scanner skips ``:eyes:`` + dispatch when every open MR is authored by ``current_user`` (#1384).

    The ``:eyes:`` reaction is a queue signal to colleagues that the
    current user is reviewing their MR. On the user's own MR broadcasts
    it is meaningless noise — the user removes it manually every time.
    The fix filters at react-time: when every open MR in the broadcast
    is authored by the configured ``current_user``, the scanner emits no
    reaction and no ``slack.review_intent`` signal.
    """

    CURRENT_USER = "adrien.cossa"
    COLLEAGUE = "colleague.dev"

    def test_skips_eyes_when_sole_open_mr_is_own(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"please review {MR_OPEN}", TS_A)]}
        states = {
            MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False, author_username=self.CURRENT_USER),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_user=self.CURRENT_USER,
        )

        signals = scanner.scan()

        assert signals == []
        assert backend.react_calls == []
        row = ScannedBroadcast.objects.get(channel=CHANNEL, slack_ts=TS_A)
        assert row.classification == ScannedBroadcast.Classification.PENDING

    def test_skips_eyes_when_all_open_mrs_are_own(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"my MRs: {MR_OPEN} {MR_OPEN_2}", TS_A)]}
        states = {
            MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False, author_username=self.CURRENT_USER),
            MR_OPEN_2: MrState(url=MR_OPEN_2, merged=False, approved=False, author_username=self.CURRENT_USER),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_user=self.CURRENT_USER,
        )

        signals = scanner.scan()

        assert signals == []
        assert backend.react_calls == []

    def test_reacts_eyes_when_any_open_mr_is_colleague(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"mixed: {MR_OPEN} {MR_OPEN_2}", TS_A)]}
        states = {
            MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False, author_username=self.CURRENT_USER),
            MR_OPEN_2: MrState(url=MR_OPEN_2, merged=False, approved=False, author_username=self.COLLEAGUE),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_user=self.CURRENT_USER,
        )

        signals = scanner.scan()

        # One own MR + one colleague MR — the colleague MR forces the
        # broadcast through the normal eyes+dispatch path. Both signals
        # emit so the dispatcher can apply its own per-MR filter.
        assert backend.react_calls == [(CHANNEL, TS_A, "eyes")]
        assert {s.payload["mr_url"] for s in signals} == {MR_OPEN, MR_OPEN_2}

    def test_legacy_no_current_user_still_reacts_eyes(self) -> None:
        # Empty ``current_user`` preserves pre-#1384 behaviour so an
        # overlay that hasn't configured a username keeps emitting the
        # queue signal on every pending broadcast.
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"please review {MR_OPEN}", TS_A)]}
        states = {
            MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False, author_username=self.CURRENT_USER),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_user="",
        )

        signals = scanner.scan()

        assert backend.react_calls == [(CHANNEL, TS_A, "eyes")]
        assert len(signals) == 1


class TestClassifierExposesAuthorUsername(TestCase):
    """``GlabGhMrStateClassifier`` reads ``author.username`` from glab JSON (#1384).

    The scanner's own-MR filter needs forge-side identity. The classifier
    is the single ingestion point — anything that doesn't surface
    ``author.username`` here cannot be filtered downstream.
    """

    def test_gitlab_classifier_surfaces_author_username(
        self,
    ) -> None:
        url = "https://gitlab.example.com/team/project/-/merge_requests/7446"

        def fake_run(cmd, *, expected_codes=None, env=None):
            _ = (expected_codes, env)
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout='{"state": "opened", "upvotes": 0, "author": {"username": "adrien.cossa"}}',
                stderr="",
            )

        with (
            patch("teatree.utils.run.run_allowed_to_fail", side_effect=fake_run),
            patch("shutil.which", return_value="/usr/bin/glab"),
        ):
            classifier = GlabGhMrStateClassifier(glab_token="glpat-fake")
            states = classifier([url])

        assert len(states) == 1
        assert states[0].author_username == "adrien.cossa"
        assert states[0].merged is False


class TestAutoAssignReviewerOnColleagueBroadcast(TestCase):
    """Scanner emits ``review_request_in_slack`` per colleague open MR (#1384 scope).

    The ``:eyes:`` reaction signals to other potential reviewers that the
    user is claiming the MR — but without also assigning the user as
    reviewer on the MR, no one picks it up and the author is blocked.
    The scanner now emits the mechanical-assign signal alongside the
    ``slack.review_intent`` dispatch signal on every colleague open MR,
    skipping own MRs (per the author==self filter from the original #1384
    fix). The existing mechanical handler
    ``assign_gitlab_reviewer`` consumes the signal and calls
    ``GitLabCodeHost.assign_reviewer`` idempotently.
    """

    CURRENT_USER = "adrien.cossa"
    COLLEAGUE = "colleague.dev"

    def test_colleague_broadcast_emits_assign_signal(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"please review {MR_OPEN}", TS_A)]}
        states = {
            MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False, author_username=self.COLLEAGUE),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_user=self.CURRENT_USER,
        )

        signals = scanner.scan()

        kinds = [s.kind for s in signals]
        assert "slack.review_intent" in kinds
        assert "review_request_in_slack" in kinds
        assign_signals = [s for s in signals if s.kind == "review_request_in_slack"]
        assert len(assign_signals) == 1
        assert assign_signals[0].payload["mr_url"] == MR_OPEN
        assert assign_signals[0].payload["reviewer_username"] == self.CURRENT_USER
        assert backend.react_calls == [(CHANNEL, TS_A, "eyes")]

    def test_own_broadcast_emits_no_assign_signal(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"my MR {MR_OPEN}", TS_A)]}
        states = {
            MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False, author_username=self.CURRENT_USER),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_user=self.CURRENT_USER,
        )

        signals = scanner.scan()

        assert [s.kind for s in signals] == []
        assert backend.react_calls == []

    def test_mixed_broadcast_assigns_only_colleague_mr(self) -> None:
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"mixed: {MR_OPEN} {MR_OPEN_2}", TS_A)]}
        states = {
            MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False, author_username=self.CURRENT_USER),
            MR_OPEN_2: MrState(url=MR_OPEN_2, merged=False, approved=False, author_username=self.COLLEAGUE),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_user=self.CURRENT_USER,
        )

        signals = scanner.scan()

        assign_signals = [s for s in signals if s.kind == "review_request_in_slack"]
        assert len(assign_signals) == 1
        assert assign_signals[0].payload["mr_url"] == MR_OPEN_2
        # Both MRs still get review-intent dispatch — the t3:reviewer
        # agent applies its own author==self skip before any review work.
        review_intents = [s for s in signals if s.kind == "slack.review_intent"]
        assert {s.payload["mr_url"] for s in review_intents} == {MR_OPEN, MR_OPEN_2}

    def test_assign_signal_skipped_when_current_user_missing(self) -> None:
        # Without a configured forge-side username, the scanner cannot
        # know who to assign; the assign signal is suppressed and only
        # the legacy review-intent dispatch fires.
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"please review {MR_OPEN}", TS_A)]}
        states = {
            MR_OPEN: MrState(url=MR_OPEN, merged=False, approved=False, author_username=self.COLLEAGUE),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_user="",
        )

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["slack.review_intent"]
        assert backend.react_calls == [(CHANNEL, TS_A, "eyes")]

    def test_all_merged_broadcast_emits_no_assign_signal(self) -> None:
        # Merged broadcasts are acknowledged with white_check_mark and
        # never need an assign — the work is done.
        backend = FakeMessaging()
        history = {CHANNEL: [_message(f"already shipped {MR_MERGED}", TS_A)]}
        states = {
            MR_MERGED: MrState(url=MR_MERGED, merged=True, approved=True, author_username=self.COLLEAGUE),
        }
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
            current_user=self.CURRENT_USER,
        )

        signals = scanner.scan()

        assert [s.kind for s in signals if s.kind == "review_request_in_slack"] == []
        assert backend.react_calls == [(CHANNEL, TS_A, "white_check_mark")]


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
