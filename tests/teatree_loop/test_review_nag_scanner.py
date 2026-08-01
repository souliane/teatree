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
from teatree.core.backend_protocols import DraftState, PrOpenState
from teatree.core.models import ReviewRequestPost
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.review.mr_state_question import mr_state_marker
from teatree.core.review.mr_triage import RepoOwner
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
    raise_on_message_read: Exception | None = None
    root_reactions: list[RawAPIDict] = field(default_factory=list)
    root_message_missing: bool = False
    usergroup_id: str = ""

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = channel
        if self.raise_on_message_read is not None:
            raise self.raise_on_message_read
        if self.root_message_missing:
            return {}
        return {"ts": ts, "reactions": list(self.root_reactions)}

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
    draft_state: DraftState = DraftState.NOT_DRAFT
    approved_by: list[str] = field(default_factory=list)
    raise_on_lookup: Exception | None = None
    raise_on_approvals: Exception | None = None
    user: str = ""
    author: str = ""

    def get_pr_open_state(self, *, pr_url: str) -> Any:
        _ = pr_url
        if self.raise_on_lookup is not None:
            raise self.raise_on_lookup
        return self.open_state

    def fetch_pr_draft_state(self, *, slug: str, pr_id: int) -> DraftState:
        _ = (slug, pr_id)
        return self.draft_state

    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> dict[str, Any]:
        _ = (repo, pr_iid)
        if self.raise_on_approvals is not None:
            raise self.raise_on_approvals
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
    nag_count: int = 0,
) -> ReviewRequestPost:
    return ReviewRequestPost.objects.create(
        mr_url=url,
        slack_channel_id=_CHANNEL,
        slack_thread_ts=thread_ts,
        created_at=timezone.now() - dt.timedelta(days=days_old),
        last_nag_at=last_nag_at,
        nag_count=nag_count,
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
        signals = ReviewNagScanner(messaging=slack, host=FakeHost(draft_state=DraftState.DRAFT)).scan()
        assert slack.posts == []
        post.refresh_from_db()
        assert post.done_at is None  # a draft may become ready later — never closed
        assert [s.kind for s in signals] == ["review_nag.mr_draft"]

    def test_unreadable_draft_state_skips_the_group_ping(self) -> None:
        """An unreadable draft flag must not @-mention the group (fail CLOSED).

        The re-ping is a colleague-visible post, so "the forge did not answer"
        cannot license nagging about an MR that may be held back as a Draft. The
        row stays open and unstamped, so the next tick retries.
        """
        post = _seed(days_old=3.0)
        slack = FakeSlack()
        signals = ReviewNagScanner(messaging=slack, host=FakeHost(draft_state=DraftState.UNKNOWN)).scan()
        assert slack.posts == []
        post.refresh_from_db()
        assert post.done_at is None
        assert post.last_nag_at is None
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


class TestUnreadableMrStateFailsClosed(_EnableReviewNagMixin, TestCase):
    """An unverifiable merge request is never @-mentioned to the group.

    The harmful direction is POSTING: every one of these paths ends in a
    colleague-visible ping about a merge request whose state nothing could
    establish. Each leaves the row open and unstamped, so the next tick retries
    once the read works again.
    """

    def test_the_same_fixture_without_the_failure_does_ping(self) -> None:
        """The control: nothing about this scanner or fake suppresses the ping on its own."""
        _seed(days_old=3.0)
        slack = FakeSlack()

        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        assert len(slack.posts) == 1
        assert [s.kind for s in signals] == ["review_nag.ping"]

    def test_unreadable_approval_state_skips_the_group_ping(self) -> None:
        """The approval probe answered neither approved nor unapproved — so no nag.

        An approval probe that reported "not approved" on a failed read would
        re-ping the group about a merge request review has already finished on.
        """
        post = _seed(days_old=3.0)
        slack = FakeSlack()
        host = FakeHost(raise_on_approvals=RuntimeError("gitlab 500"))

        signals = ReviewNagScanner(messaging=slack, host=host).scan()

        assert slack.posts == []
        post.refresh_from_db()
        assert post.done_at is None
        assert post.last_nag_at is None
        assert post.nag_count == 0
        assert [s.kind for s in signals] == ["review_nag.mr_approval_unknown"]

    def test_unknown_open_state_skips_the_group_ping(self) -> None:
        post = _seed(days_old=3.0)
        slack = FakeSlack()

        signals = ReviewNagScanner(messaging=slack, host=FakeHost(open_state=PrOpenState.UNKNOWN)).scan()

        assert slack.posts == []
        post.refresh_from_db()
        assert post.done_at is None
        assert post.last_nag_at is None
        assert [s.kind for s in signals] == ["review_nag.mr_state_unreadable"]

    def test_open_state_lookup_failure_skips_the_group_ping(self) -> None:
        _seed(days_old=3.0)
        slack = FakeSlack()
        host = FakeHost(raise_on_lookup=RuntimeError("gitlab 500"))

        signals = ReviewNagScanner(messaging=slack, host=host).scan()

        assert slack.posts == []
        assert [s.kind for s in signals] == ["review_nag.mr_state_unreadable"]

    def test_no_host_skips_the_group_ping(self) -> None:
        _seed(days_old=3.0)
        slack = FakeSlack()

        signals = ReviewNagScanner(messaging=slack, host=None).scan()

        assert slack.posts == []
        assert [s.kind for s in signals] == ["review_nag.mr_state_unreadable"]

    def test_an_unparsable_url_skips_the_group_ping(self) -> None:
        """Neither the draft nor the approval probe can even be formed for it."""
        _seed(url="not-a-url", days_old=3.0)
        slack = FakeSlack()

        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        assert slack.posts == []
        assert [s.kind for s in signals] == ["review_nag.mr_state_unreadable"]


class TestPausedRequestIsNeverPinged(_EnableReviewNagMixin, TestCase):
    """A request the owner reacted "hold" on is theirs to release, not ours to escalate."""

    def test_a_paused_request_is_not_nagged(self) -> None:
        post = _seed(days_old=10.0)
        slack = FakeSlack(root_reactions=[{"name": "double_vertical_bar", "count": 1}])

        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        assert slack.posts == []
        post.refresh_from_db()
        assert post.done_at is None
        assert post.last_nag_at is None
        assert post.nag_count == 0
        assert [s.kind for s in signals] == ["review_nag.mr_paused"]

    def test_an_unreadable_pause_state_is_not_nagged(self) -> None:
        _seed(days_old=10.0)
        slack = FakeSlack(raise_on_message_read=RuntimeError("slack down"))

        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        assert slack.posts == []
        assert [s.kind for s in signals] == ["review_nag.mr_paused"]

    def test_an_empty_root_message_is_not_nagged(self) -> None:
        _seed(days_old=10.0)
        slack = FakeSlack(root_message_missing=True)

        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        assert slack.posts == []
        assert [s.kind for s in signals] == ["review_nag.mr_paused"]

    def test_an_unrelated_reaction_on_the_root_does_not_hold_the_nag(self) -> None:
        """The control: only a configured PAUSE reaction holds it."""
        _seed(days_old=10.0)
        slack = FakeSlack(root_reactions=[{"name": "tada", "count": 1}])

        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        assert len(slack.posts) == 1
        assert [s.kind for s in signals] == ["review_nag.ping"]


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
        post = _seed(days_old=10.0)
        ReviewRequestPost.objects.filter(pk=post.pk).update(done_at=timezone.now())
        slack = FakeSlack()
        assert ReviewNagScanner(messaging=slack, host=FakeHost()).scan() == []

    def test_no_messaging_backend_is_a_noop(self) -> None:
        _seed(days_old=3.0)
        assert ReviewNagScanner(messaging=None, host=FakeHost()).scan() == []

    def test_scanner_name(self) -> None:
        assert ReviewNagScanner(messaging=FakeSlack()).name == "review_nag"

    def test_multiple_rows_each_pinged(self) -> None:
        _seed(url="https://gitlab.example/x/-/merge_requests/11", thread_ts="ts.A", days_old=3.0)
        _seed(url="https://gitlab.example/x/-/merge_requests/12", thread_ts="ts.B", days_old=4.0)
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


class TestNagPatienceFollowsTheRepoOwner(_EnableReviewNagMixin, TestCase):
    """DevOps review on their own rota, so the engineering interval is noise to them.

    The interval is the only thing that varies — the mention and the text are the
    same wherever the MR lives.
    """

    _DEVOPS_MR = "https://gitlab.example/group/helm-charts/-/merge_requests/3"

    @staticmethod
    def _devops_everywhere(_slug: str) -> RepoOwner:
        return RepoOwner.DEVOPS

    def test_an_engineering_repo_still_pings_after_two_idle_days(self) -> None:
        _seed(days_old=3.0)
        slack = FakeSlack()

        ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        assert len(slack.posts) == 1

    def test_a_devops_repo_is_still_waiting_at_three_idle_days(self) -> None:
        _seed(url=self._DEVOPS_MR, days_old=3.0)
        slack = FakeSlack()

        signals = ReviewNagScanner(messaging=slack, host=FakeHost(), repo_owner=self._devops_everywhere).scan()

        assert slack.posts == []
        assert signals == []

    def test_a_devops_repo_pings_once_its_own_window_passes(self) -> None:
        _seed(url=self._DEVOPS_MR, days_old=6.0)
        slack = FakeSlack()

        ReviewNagScanner(messaging=slack, host=FakeHost(), repo_owner=self._devops_everywhere).scan()

        assert len(slack.posts) == 1

    def test_the_patient_owner_re_pings_on_its_own_interval_too(self) -> None:
        """``last_nag_at`` is read against the same interval, not a fixed two days."""
        _seed(url=self._DEVOPS_MR, days_old=30.0, last_nag_at=timezone.now() - dt.timedelta(days=3))
        slack = FakeSlack()

        ReviewNagScanner(messaging=slack, host=FakeHost(), repo_owner=self._devops_everywhere).scan()

        assert slack.posts == []

    def test_the_text_is_the_same_whoever_owns_the_repo(self) -> None:
        _seed(url=self._DEVOPS_MR, days_old=6.0)
        slack = FakeSlack()

        ReviewNagScanner(messaging=slack, host=FakeHost(), repo_owner=self._devops_everywhere).scan()

        assert slack.posts[0]["text"] == "@engineers :pray:"

    def test_the_resolver_is_asked_about_the_repo_slug_not_the_url(self) -> None:
        seen: list[str] = []

        def _record(slug: str) -> RepoOwner:
            seen.append(slug)
            return RepoOwner.ENGINEERING

        _seed(url=self._DEVOPS_MR, days_old=3.0)
        ReviewNagScanner(messaging=FakeSlack(), host=FakeHost(), repo_owner=_record).scan()

        assert seen == ["group/helm-charts"]

    def test_an_unparsable_url_asks_about_the_empty_slug(self) -> None:
        """The overlay owns the fail-safe for an unknown repo; core must not preempt it."""
        seen: list[str] = []

        def _record(slug: str) -> RepoOwner:
            seen.append(slug)
            return RepoOwner.DEVOPS

        _seed(url="not-a-url", days_old=3.0)
        ReviewNagScanner(messaging=FakeSlack(), host=FakeHost(), repo_owner=_record).scan()

        assert seen == [""]


class TestSuccessiveNagsWidenOnTheFibonacciSchedule(_EnableReviewNagMixin, TestCase):
    """Each unanswered re-ask waits longer than the one before it.

    The engineering base is 2 days and the steps are 1, 1, 2, 3 — so the gaps
    run 2, 2, 4, 6 days. Each row is probed an hour BEFORE its window closes as
    well as after, so a schedule that widened too little would post early and
    fail here rather than pass on the later assertion alone.
    """

    _START = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    _GAP_DAYS = (2, 2, 4, 6)

    def test_the_gap_between_re_asks_follows_the_schedule(self) -> None:
        post = ReviewRequestPost.objects.create(
            mr_url="https://gitlab.example/x/-/merge_requests/7",
            slack_channel_id=_CHANNEL,
            slack_thread_ts="ts.7",
            created_at=self._START - dt.timedelta(days=100),
            last_nag_at=self._START,
        )
        slack = FakeSlack()
        moment = self._START

        for nags_so_far, gap_days in enumerate(self._GAP_DAYS):
            just_short = moment + dt.timedelta(days=gap_days, hours=-1)
            ReviewNagScanner(messaging=slack, host=FakeHost(), now=just_short).scan()
            assert len(slack.posts) == nags_so_far, f"posted early at gap {gap_days}d"

            moment += dt.timedelta(days=gap_days, hours=1)
            ReviewNagScanner(messaging=slack, host=FakeHost(), now=moment).scan()
            assert len(slack.posts) == nags_so_far + 1, f"did not post after gap {gap_days}d"
            post.refresh_from_db()
            assert post.nag_count == nags_so_far + 1


class TestNagCountRollsBackWithTheClaim(_EnableReviewNagMixin, TestCase):
    """A nag that never reached Slack must not advance the backoff.

    ``nag_count`` is claimed in the same conditional ``UPDATE`` as
    ``last_nag_at``, so a post that is blocked or fails in transport releases
    both — otherwise a permanently-gated row would widen its own window to the
    ceiling without a single re-ask ever having been delivered.
    """

    def test_a_blocked_post_restores_both_the_stamp_and_the_count(self) -> None:
        post = _seed(days_old=30.0, nag_count=2)
        slack = FakeSlack()
        gated = TeaTreeConfig(
            user=UserSettings(review_nag_enabled=True, on_behalf_post_mode=OnBehalfPostMode.ASK),
        )

        with patch("teatree.config.load_config", return_value=gated):
            signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        assert slack.posts == []
        post.refresh_from_db()
        assert post.last_nag_at is None
        assert post.nag_count == 2
        assert [s.kind for s in signals] == ["review_nag.gated"]

    def test_a_failed_post_restores_both_the_stamp_and_the_count(self) -> None:
        post = _seed(days_old=30.0, nag_count=2)
        slack = FakeSlack(raise_on_post=RuntimeError("not_in_channel"))

        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        post.refresh_from_db()
        assert post.last_nag_at is None
        assert post.nag_count == 2
        assert [s.kind for s in signals] == ["review_nag.post_failed"]

    def test_a_delivered_re_ask_advances_the_count(self) -> None:
        """The control: the two rollbacks above are not asserting a count that never moves."""
        post = _seed(days_old=30.0, nag_count=2)

        ReviewNagScanner(messaging=FakeSlack(), host=FakeHost()).scan()

        post.refresh_from_db()
        assert post.nag_count == 3
        assert post.last_nag_at is not None


class TestBackoffCapAsksTheOwnerInsteadOfNaggingForever(_EnableReviewNagMixin, TestCase):
    """Past the ceiling the group has been asked enough — the owner is asked instead."""

    def test_at_the_cap_a_question_is_recorded_and_nothing_is_posted(self) -> None:
        post = _seed(days_old=200.0, nag_count=7)
        slack = FakeSlack()

        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        assert slack.posts == []
        post.refresh_from_db()
        assert post.last_nag_at is None
        assert post.nag_count == 7
        question = DeferredQuestion.objects.get(dedupe_marker=mr_state_marker(post.mr_url))
        assert post.mr_url in question.question
        assert [s.kind for s in signals] == ["review_nag.backoff_exhausted"]

    def test_one_step_below_the_cap_still_re_asks_the_group(self) -> None:
        """The control: the cap, not the fixture's age or count, is what stops the nag."""
        _seed(days_old=200.0, nag_count=6)
        slack = FakeSlack()

        signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        assert len(slack.posts) == 1
        assert DeferredQuestion.objects.count() == 0
        assert [s.kind for s in signals] == ["review_nag.ping"]

    def test_a_lower_configured_ceiling_stops_the_nag_sooner(self) -> None:
        """``review_nag_max_interval_days`` is the ceiling — the reader is live, not a constant."""
        _seed(days_old=200.0, nag_count=2)
        slack = FakeSlack()
        tight = TeaTreeConfig(
            user=UserSettings(
                review_nag_enabled=True,
                on_behalf_post_mode=OnBehalfPostMode.IMMEDIATE,
                review_nag_max_interval_days=3,
            ),
        )

        with patch("teatree.config.load_config", return_value=tight):
            signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        assert slack.posts == []
        assert [s.kind for s in signals] == ["review_nag.backoff_exhausted"]

    def test_a_refused_question_leaves_the_row_untouched_for_a_later_tick(self) -> None:
        post = _seed(days_old=200.0, nag_count=7)
        slack = FakeSlack()
        no_slots = TeaTreeConfig(
            user=UserSettings(
                review_nag_enabled=True,
                on_behalf_post_mode=OnBehalfPostMode.IMMEDIATE,
                mr_state_questions_max_per_tick=0,
            ),
        )

        with patch("teatree.config.load_config", return_value=no_slots):
            signals = ReviewNagScanner(messaging=slack, host=FakeHost()).scan()

        assert slack.posts == []
        assert DeferredQuestion.objects.count() == 0
        post.refresh_from_db()
        assert post.nag_count == 7
        assert signals == []


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
