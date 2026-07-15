"""Tests for the ReviewNagScanner — 2-day ``@engineers :pray:`` re-ping (#1084 follow-up).

The scanner walks ``ReviewRequestPost`` rows and, when an MR has had no thread
activity (reply or reaction) for 2 days and is still live-open, non-draft, and
unapproved, posts exactly ONE thread reply mentioning ``@engineers`` + `` :pray:``.
``last_nag_at`` enforces no double-ping within the 2-day window.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.config import OnBehalfPostMode, TeaTreeConfig, UserSettings
from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import ReviewRequestPost
from teatree.loop.scanners.review_nag import ReviewNagScanner
from teatree.types import RawAPIDict

_CHANNEL = "C0DEMOCHAN1"


def _recent_ts(hours: float) -> str:
    return f"{(timezone.now() - dt.timedelta(hours=hours)).timestamp():.6f}"


class _EnableReviewNagMixin:
    """Flip ``review_nag_enabled`` ON (and the on-behalf gate to IMMEDIATE) per test."""

    def setUp(self) -> None:
        super().setUp()
        enabled = TeaTreeConfig(
            user=UserSettings(review_nag_enabled=True, on_behalf_post_mode=OnBehalfPostMode.IMMEDIATE),
        )
        patcher = patch("teatree.config.load_config", return_value=enabled)
        patcher.start()
        self.addCleanup(patcher.stop)


@dataclass
class FakeSlack:
    """In-memory ``MessagingBackend`` recording posts; ``thread_replies`` drives activity."""

    thread_replies: list[RawAPIDict] = field(default_factory=list)
    posts: list[dict[str, Any]] = field(default_factory=list)
    reactions: list[dict[str, Any]] = field(default_factory=list)
    raise_on_post: Exception | None = None
    raise_on_resolve: Exception | None = None
    raise_on_thread_read: Exception | None = None
    usergroup_id: str = ""

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_thread_replies(self, *, channel: str, thread_ts: str) -> list[RawAPIDict]:
        _ = (channel, thread_ts)
        if self.raise_on_thread_read is not None:
            raise self.raise_on_thread_read
        return list(self.thread_replies)

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        if self.raise_on_post is not None:
            raise self.raise_on_post
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": f"reply.{len(self.posts)}"}

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        return self.post_message(channel=channel, text=text, thread_ts=thread_ts)

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        return self.post_message(channel=channel, text=text, thread_ts=ts)

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        return "D-USER"

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example/archives/{channel}/p{ts}"

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = (channel, ts, emoji)
        return {}

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reactions.append({"channel": channel, "ts": ts, "emoji": emoji})
        return {"ok": True}

    def resolve_user_id(self, handle: str) -> str:
        if self.raise_on_resolve is not None:
            raise self.raise_on_resolve
        return self.usergroup_id if handle == "engineers" else ""


@dataclass
class FakeHost:
    """In-memory ``CodeHostBackend`` returning a fixed open-state / draft / approval."""

    open_state: Any = PrOpenState.OPEN
    draft: bool = False
    approved_by: list[str] = field(default_factory=list)
    raise_on_lookup: Exception | None = None
    user: str = ""
    author: str = ""

    def get_pr_open_state(self, *, pr_url: str) -> Any:
        _ = pr_url
        if self.raise_on_lookup is not None:
            raise self.raise_on_lookup
        return self.open_state

    def fetch_pr_is_draft(self, *, slug: str, pr_id: int) -> bool:
        _ = (slug, pr_id)
        return self.draft

    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> dict[str, Any]:
        _ = (repo, pr_iid)
        return {
            "approvals_left": 0 if self.approved_by else 1,
            "approved_by": self.approved_by,
            "unresolved_resolvable": 0,
        }

    def current_user(self) -> str:
        return self.user

    def get_pr_author(self, *, pr_url: str) -> str:
        _ = pr_url
        return self.author


def _seed(
    *,
    url: str = "https://gitlab.example/x/-/merge_requests/1",
    thread_ts: str = "ts.1",
    days_old: float = 3.0,
    last_nag_at: dt.datetime | None = None,
    done_at: dt.datetime | None = None,
) -> ReviewRequestPost:
    return ReviewRequestPost.objects.create(
        mr_url=url,
        slack_channel_id=_CHANNEL,
        slack_thread_ts=thread_ts,
        created_at=timezone.now() - dt.timedelta(days=days_old),
        last_nag_at=last_nag_at,
        done_at=done_at,
    )


class TestActivityGate(_EnableReviewNagMixin, TestCase):
    def test_idle_over_two_days_pings_engineers_on_thread(self) -> None:
        post = _seed(days_old=3.0)
        slack = FakeSlack()
        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        assert len(slack.posts) == 1
        sent = slack.posts[0]
        assert sent["channel"] == _CHANNEL
        assert sent["thread_ts"] == "ts.1"
        assert sent["text"] == "@engineers :pray:"
        post.refresh_from_db()
        assert post.last_nag_at is not None
        assert [s.kind for s in signals] == ["review_nag.ping"]

    def test_fresh_post_within_two_days_does_not_ping(self) -> None:
        _seed(days_old=1.0)
        slack = FakeSlack()
        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()
        assert slack.posts == []
        assert signals == []

    def test_recent_thread_reply_suppresses_the_ping(self) -> None:
        _seed(days_old=5.0)
        slack = FakeSlack(thread_replies=[{"ts": _recent_ts(1)}])  # a reply 1h ago == activity
        ReviewNagScanner(messaging=slack, host=FakeHost()).scan()
        assert slack.posts == []

    def test_reaction_on_thread_suppresses_the_ping(self) -> None:
        _seed(days_old=5.0)
        slack = FakeSlack(thread_replies=[{"ts": "ts.parent", "reactions": [{"name": "eyes", "count": 1}]}])
        ReviewNagScanner(messaging=slack, host=FakeHost()).scan()
        assert slack.posts == []

    def test_thread_read_failure_skips_this_tick(self) -> None:
        _seed(days_old=5.0)
        slack = FakeSlack(raise_on_thread_read=RuntimeError("slack down"))
        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()
        assert slack.posts == []
        assert signals == []


class TestNoDoublePing(_EnableReviewNagMixin, TestCase):
    def test_recent_last_nag_at_blocks_re_ping(self) -> None:
        _seed(days_old=10.0, last_nag_at=timezone.now() - dt.timedelta(hours=6))
        slack = FakeSlack()
        assert ReviewNagScanner(messaging=slack, host=FakeHost()).scan() == []
        assert slack.posts == []

    def test_re_pings_again_after_two_more_idle_days(self) -> None:
        post = _seed(days_old=10.0, last_nag_at=timezone.now() - dt.timedelta(days=2, hours=1))
        slack = FakeSlack()
        ReviewNagScanner(messaging=slack, host=FakeHost()).scan()
        assert len(slack.posts) == 1
        post.refresh_from_db()
        assert timezone.now() - post.last_nag_at < dt.timedelta(minutes=1)

    def test_double_scan_in_same_window_pings_once(self) -> None:
        _seed(days_old=3.0)
        slack = FakeSlack()
        scanner = ReviewNagScanner(messaging=slack, host=FakeHost())
        scanner.scan()
        scanner.scan()
        assert len(slack.posts) == 1


class TestMention(_EnableReviewNagMixin, TestCase):
    def test_subteam_mention_when_usergroup_resolves(self) -> None:
        _seed(days_old=3.0)
        slack = FakeSlack(usergroup_id="S_ENG")
        ReviewNagScanner(messaging=slack, host=FakeHost()).scan()
        assert slack.posts[0]["text"] == "<!subteam^S_ENG> :pray:"

    def test_resolve_failure_falls_back_to_plain_handle(self) -> None:
        _seed(days_old=3.0)
        slack = FakeSlack(raise_on_resolve=RuntimeError("api down"))
        ReviewNagScanner(messaging=slack, host=FakeHost()).scan()
        assert slack.posts[0]["text"] == "@engineers :pray:"


class TestMrStateGate(_EnableReviewNagMixin, TestCase):
    def test_merged_mr_reacts_and_closes_without_pinging(self) -> None:
        post = _seed(days_old=3.0)
        slack = FakeSlack()
        host = FakeHost(open_state=PrOpenState.MERGED, author="a-colleague")
        signals = ReviewNagScanner(messaging=slack, host=host, identities=("souliane",)).scan()
        assert slack.posts == []
        assert slack.reactions == [{"channel": _CHANNEL, "ts": "ts.1", "emoji": "merge"}]
        post.refresh_from_db()
        assert post.done_at is not None
        assert [s.kind for s in signals] == ["review_request_merge_react.reacted"]

    def test_closed_mr_marks_done_without_pinging(self) -> None:
        post = _seed(days_old=3.0)
        slack = FakeSlack()
        signals = ReviewNagScanner(messaging=slack, host=FakeHost(open_state=PrOpenState.CLOSED)).scan()
        assert slack.posts == []
        post.refresh_from_db()
        assert post.done_at is not None
        assert [s.kind for s in signals] == ["review_nag.mr_closed"]

    def test_draft_mr_is_skipped_not_closed(self) -> None:
        post = _seed(days_old=3.0)
        slack = FakeSlack()
        signals = ReviewNagScanner(messaging=slack, host=FakeHost(draft=True)).scan()
        assert slack.posts == []
        post.refresh_from_db()
        assert post.done_at is None  # a draft may become ready later — never closed
        assert [s.kind for s in signals] == ["review_nag.mr_draft"]

    def test_approved_mr_is_skipped_not_closed(self) -> None:
        post = _seed(days_old=3.0)
        slack = FakeSlack()
        signals = ReviewNagScanner(messaging=slack, host=FakeHost(approved_by=["reviewer"])).scan()
        assert slack.posts == []
        post.refresh_from_db()
        assert post.done_at is None  # left open so a later merge-react still fires
        assert [s.kind for s in signals] == ["review_nag.mr_approved"]

    def test_open_non_draft_unapproved_pings(self) -> None:
        _seed(days_old=3.0)
        slack = FakeSlack()
        signals = ReviewNagScanner(messaging=slack, host=FakeHost(open_state=PrOpenState.OPEN)).scan()
        assert len(slack.posts) == 1
        assert [s.kind for s in signals] == ["review_nag.ping"]

    def test_unknown_state_fails_open_and_pings(self) -> None:
        _seed(days_old=3.0)
        slack = FakeSlack()
        ReviewNagScanner(messaging=slack, host=FakeHost(open_state=PrOpenState.UNKNOWN)).scan()
        assert len(slack.posts) == 1

    def test_lookup_failure_fails_open_and_pings(self) -> None:
        _seed(days_old=3.0)
        slack = FakeSlack()
        host = FakeHost(raise_on_lookup=RuntimeError("gitlab 500"))
        ReviewNagScanner(messaging=slack, host=host).scan()
        assert len(slack.posts) == 1

    def test_no_host_fails_open_and_pings(self) -> None:
        _seed(days_old=3.0)
        slack = FakeSlack()
        ReviewNagScanner(messaging=slack, host=None).scan()
        assert len(slack.posts) == 1


class TestPostFailure(_EnableReviewNagMixin, TestCase):
    def test_not_in_channel_releases_claim_and_reports_failure(self) -> None:
        post = _seed(days_old=3.0)
        slack = FakeSlack(raise_on_post=RuntimeError("not_in_channel"))
        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()
        post.refresh_from_db()
        assert post.last_nag_at is None  # claim released so a future tick retries
        assert [s.kind for s in signals] == ["review_nag.post_failed"]


class TestConcurrentTickPingsOnce(_EnableReviewNagMixin, TestCase):
    def test_two_snapshots_ping_once(self) -> None:
        _seed(days_old=3.0, thread_ts="ts.77")
        scanner = ReviewNagScanner(messaging=FakeSlack(), host=FakeHost())
        snap_a = ReviewRequestPost.objects.get(slack_thread_ts="ts.77")
        snap_b = ReviewRequestPost.objects.get(slack_thread_ts="ts.77")
        slack = FakeSlack()
        right_now = timezone.now()
        with patch("teatree.core.gates.review_request_guard.resolve_guard_target", return_value=None):
            sig_a = scanner._post_engineers_pray(snap_a, slack, right_now)
            sig_b = scanner._post_engineers_pray(snap_b, slack, right_now)
        assert len(slack.posts) == 1
        assert [s.kind for s in (sig_a, sig_b) if s is not None] == ["review_nag.ping"]


class TestReconcileBeforeNag(_EnableReviewNagMixin, TestCase):
    def test_out_of_band_reconcile_skips_the_ping(self) -> None:
        from teatree.core.gates.review_request_guard import GuardTarget  # noqa: PLC0415

        _seed(days_old=3.0)
        slack = FakeSlack()
        target = GuardTarget(channel_id=_CHANNEL, channel_name="rev", token="xoxb")
        with (
            patch("teatree.core.gates.review_request_guard.resolve_guard_target", return_value=target),
            patch(
                "teatree.core.gates.review_request_guard.reconcile_out_of_band",
                return_value="https://team.slack.com/archives/C/p1",
            ),
        ):
            signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()
        assert slack.posts == []
        assert any(s.kind == "review_nag.reconciled" for s in signals)


class TestMisc(_EnableReviewNagMixin, TestCase):
    def test_done_row_is_skipped(self) -> None:
        _seed(days_old=10.0, done_at=timezone.now())
        slack = FakeSlack()
        assert ReviewNagScanner(messaging=slack, host=FakeHost()).scan() == []

    def test_no_messaging_backend_is_a_noop(self) -> None:
        _seed(days_old=3.0)
        assert ReviewNagScanner(messaging=None, host=FakeHost()).scan() == []

    def test_scanner_name(self) -> None:
        assert ReviewNagScanner(messaging=FakeSlack()).name == "review_nag"

    def test_multiple_rows_each_pinged(self) -> None:
        _seed(url="https://gitlab.example/x/-/merge_requests/A", thread_ts="ts.A", days_old=3.0)
        _seed(url="https://gitlab.example/x/-/merge_requests/B", thread_ts="ts.B", days_old=4.0)
        slack = FakeSlack()
        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()
        assert {p["thread_ts"] for p in slack.posts} == {"ts.A", "ts.B"}
        assert [s.kind for s in signals] == ["review_nag.ping", "review_nag.ping"]


class TestCustomNow(_EnableReviewNagMixin, TestCase):
    def test_now_override_gates_the_two_day_window(self) -> None:
        ReviewRequestPost.objects.create(
            mr_url="https://gitlab.example/x/-/merge_requests/99",
            slack_channel_id=_CHANNEL,
            slack_thread_ts="ts.99",
            created_at=dt.datetime(2026, 5, 1, 12, 0, tzinfo=dt.UTC),
        )
        slack = FakeSlack()
        # +1 day → within window → no ping.
        ReviewNagScanner(messaging=slack, host=FakeHost(), now=dt.datetime(2026, 5, 2, 12, 0, tzinfo=dt.UTC)).scan()
        assert slack.posts == []
        # +3 days → past the window → ping.
        ReviewNagScanner(messaging=slack, host=FakeHost(), now=dt.datetime(2026, 5, 4, 12, 0, tzinfo=dt.UTC)).scan()
        assert len(slack.posts) == 1


class TestDisabledByDefault(TestCase):
    def test_disabled_flag_makes_scan_a_noop(self) -> None:
        disabled = TeaTreeConfig(user=UserSettings(review_nag_enabled=False))
        _seed(days_old=3.0)
        slack = FakeSlack()
        with patch("teatree.config.load_config", return_value=disabled):
            assert ReviewNagScanner(messaging=slack, host=FakeHost()).scan() == []
        assert slack.posts == []

    def test_default_user_settings_disable_the_nag(self) -> None:
        assert UserSettings().review_nag_enabled is False
