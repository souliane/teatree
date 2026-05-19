"""Tests for the ReviewNagScanner (#1038).

The scanner walks ``ReviewRequestPost`` rows and posts a fibonacci-cadence
thread reply on the original review-request message when the MR has not
been picked up after +1, +2, +3, +5 days. After +5 days with no reviewer
it DMs the user and marks the row done.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ReviewRequestPost
from teatree.loop.scanners.review_nag import ReviewNagScanner, fibonacci_step_for_age
from teatree.types import RawAPIDict


@dataclass
class FakeSlack:
    """In-memory MessagingBackend for testing."""

    posts: list[dict[str, Any]] = field(default_factory=list)
    raise_on_post: Exception | None = None
    raise_on_resolve: Exception | None = None
    raise_on_open_dm: Exception | None = None
    usergroup_id: str = ""
    dm_channel: str = "D-USER"

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        return []

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        if self.raise_on_post is not None:
            raise self.raise_on_post
        self.posts.append({"channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ok": True, "ts": f"reply.{len(self.posts)}"}

    def post_reply(self, *, channel: str, ts: str, text: str) -> RawAPIDict:
        return self.post_message(channel=channel, text=text, thread_ts=ts)

    def open_dm(self, user_id: str) -> str:
        _ = user_id
        if self.raise_on_open_dm is not None:
            raise self.raise_on_open_dm
        return self.dm_channel

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example/archives/{channel}/p{ts}"

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        _ = (channel, ts, emoji)
        return {}

    def resolve_user_id(self, handle: str) -> str:
        if self.raise_on_resolve is not None:
            raise self.raise_on_resolve
        # Maps "engineers" → "S_ENG" if usergroup is configured.
        if handle == "engineers":
            return self.usergroup_id
        return ""


class TestFibonacciStepCalculation(TestCase):
    """Pure-logic test for the age → step mapping."""

    def test_under_one_day_is_step_zero(self) -> None:
        assert fibonacci_step_for_age(dt.timedelta(hours=23)) == 0

    def test_one_day_is_step_one(self) -> None:
        assert fibonacci_step_for_age(dt.timedelta(days=1, hours=1)) == 1

    def test_two_days_is_step_two(self) -> None:
        assert fibonacci_step_for_age(dt.timedelta(days=2, hours=1)) == 2

    def test_three_days_is_step_three(self) -> None:
        assert fibonacci_step_for_age(dt.timedelta(days=3, hours=1)) == 3

    def test_four_days_is_still_step_three(self) -> None:
        # No fibonacci step at +4d — the cadence is 1/2/3/5.
        assert fibonacci_step_for_age(dt.timedelta(days=4, hours=12)) == 3

    def test_five_days_is_step_four(self) -> None:
        assert fibonacci_step_for_age(dt.timedelta(days=5, hours=1)) == 4

    def test_six_days_is_still_step_four(self) -> None:
        # +6d stays on the terminal step — the scanner uses last_nag_step==4
        # as the "DM user and stop" trigger.
        assert fibonacci_step_for_age(dt.timedelta(days=6)) == 4


class TestReviewNagScanner(TestCase):
    """Behaviour tests for the fibonacci nag scanner.

    All tests pre-create ``ReviewRequestPost`` rows directly — production
    behaviour for *creating* those rows is covered by the model tests and
    the seeder integration. The scanner's job is purely to walk existing
    rows and decide whether to nag, dm, or skip.
    """

    def _seed_post(self, **overrides: Any) -> ReviewRequestPost:
        spec: dict[str, Any] = {
            "url": "https://gitlab.example/x/-/merge_requests/1",
            "channel": "C0DEMOCHAN1",
            "thread_ts": "1700000000.001",
            "days_old": 0.0,
            "last_nag_step": 0,
            "done_at": None,
        }
        spec.update(overrides)
        created_at = timezone.now() - dt.timedelta(days=spec["days_old"])
        return ReviewRequestPost.objects.create(
            mr_url=spec["url"],
            slack_channel_id=spec["channel"],
            slack_thread_ts=spec["thread_ts"],
            created_at=created_at,
            last_nag_step=spec["last_nag_step"],
            done_at=spec["done_at"],
        )

    def test_fresh_post_under_one_day_does_not_nag(self) -> None:
        self._seed_post(days_old=0.5, last_nag_step=0)
        slack = FakeSlack()
        scanner = ReviewNagScanner(messaging=slack, user_slack_id="U_ME")
        signals = scanner.scan()
        assert signals == []
        assert slack.posts == []

    def test_one_day_old_unreviewed_pings_engineers_as_thread_reply(self) -> None:
        post = self._seed_post(days_old=1.2, last_nag_step=0)
        slack = FakeSlack()
        scanner = ReviewNagScanner(messaging=slack, user_slack_id="U_ME")
        signals = scanner.scan()

        assert len(slack.posts) == 1
        sent = slack.posts[0]
        assert sent["channel"] == "C0DEMOCHAN1"
        assert sent["thread_ts"] == "1700000000.001"
        assert "merge_requests/1" in sent["text"]
        assert "day 1 of 5" in sent["text"]
        assert "@engineers" in sent["text"]

        post.refresh_from_db()
        assert post.last_nag_step == 1
        assert post.done_at is None

        assert [s.kind for s in signals] == ["review_nag.ping"]

    def test_subteam_mention_used_when_usergroup_resolves(self) -> None:
        self._seed_post(days_old=1.2, last_nag_step=0)
        slack = FakeSlack(usergroup_id="S_ENG")
        scanner = ReviewNagScanner(messaging=slack, user_slack_id="U_ME")
        scanner.scan()

        sent = slack.posts[0]
        assert "<!subteam^S_ENG>" in sent["text"]
        assert "@engineers" not in sent["text"]

    def test_usergroup_lookup_failure_falls_back_to_plain_text(self) -> None:
        """A raised ``resolve_user_id`` must NOT crash the nag — fall back to ``@engineers``."""
        slack = FakeSlack(raise_on_resolve=RuntimeError("slack api down"))
        self._seed_post(days_old=1.2, last_nag_step=0)
        ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()
        sent = slack.posts[0]
        assert "@engineers" in sent["text"]

    def test_idempotent_double_scan_in_same_window_posts_once(self) -> None:
        self._seed_post(days_old=1.2, last_nag_step=0)
        slack = FakeSlack()
        scanner = ReviewNagScanner(messaging=slack, user_slack_id="U_ME")
        scanner.scan()
        scanner.scan()
        assert len(slack.posts) == 1

    def test_two_day_old_after_day_one_ping_pings_again(self) -> None:
        # Already pinged step 1; now at +2d, should bump to step 2.
        post = self._seed_post(days_old=2.1, last_nag_step=1)
        slack = FakeSlack()
        ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()
        assert len(slack.posts) == 1
        assert "day 2 of 5" in slack.posts[0]["text"]
        post.refresh_from_db()
        assert post.last_nag_step == 2

    def test_three_day_old_after_day_two_pings_step_three(self) -> None:
        post = self._seed_post(days_old=3.1, last_nag_step=2)
        slack = FakeSlack()
        ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()
        assert "day 3 of 5" in slack.posts[0]["text"]
        post.refresh_from_db()
        assert post.last_nag_step == 3

    def test_five_day_old_after_day_three_pings_step_four(self) -> None:
        post = self._seed_post(days_old=5.1, last_nag_step=3)
        slack = FakeSlack()
        ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()
        assert "day 5 of 5" in slack.posts[0]["text"]
        post.refresh_from_db()
        assert post.last_nag_step == 4

    def test_six_day_old_after_step_four_dms_user_and_marks_done(self) -> None:
        post = self._seed_post(days_old=6.0, last_nag_step=4)
        slack = FakeSlack()
        signals = ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()

        # Posts exactly once — the user DM. No further thread ping.
        assert len(slack.posts) == 1
        sent = slack.posts[0]
        assert sent["channel"] == "D-USER"  # DM channel from open_dm
        assert "merge_requests/1" in sent["text"]
        assert "long-stale" in sent["text"].lower()

        post.refresh_from_db()
        assert post.done_at is not None
        assert post.last_nag_step == 4
        assert [s.kind for s in signals] == ["review_nag.stale_dm"]

    def test_done_row_is_skipped_entirely(self) -> None:
        self._seed_post(days_old=10.0, last_nag_step=4, done_at=timezone.now())
        slack = FakeSlack()
        signals = ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()
        assert slack.posts == []
        assert signals == []

    def test_backfill_marks_historic_rows_done_without_posting(self) -> None:
        """A row with created_at older than 5d but last_nag_step==0 is historic.

        The model was added after the post was made. We don't have a way
        to retroactively know which fibonacci pings should have fired, so
        mark the row as done (step=4, done_at=now) and skip — never spam
        history.
        """
        post = self._seed_post(days_old=10.0, last_nag_step=0)
        slack = FakeSlack()
        signals = ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()
        assert slack.posts == []
        post.refresh_from_db()
        assert post.last_nag_step == 4
        assert post.done_at is not None
        assert [s.kind for s in signals] == ["review_nag.backfill_skip"]

    def test_not_in_channel_error_is_caught_and_row_left_alone(self) -> None:
        """A Slack-Connect channel the bot isn't in raises ``not_in_channel``.

        The scanner must catch and log without crashing, and must NOT
        bump last_nag_step (so a future re-invitation lets the ping
        finally land).
        """
        post = self._seed_post(days_old=1.2, last_nag_step=0)
        slack = FakeSlack(raise_on_post=RuntimeError("not_in_channel"))
        signals = ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()

        post.refresh_from_db()
        # last_nag_step UNCHANGED — the post failed.
        assert post.last_nag_step == 0
        assert post.done_at is None
        # Scanner reports the failure as a signal but does not crash.
        assert [s.kind for s in signals] == ["review_nag.post_failed"]

    def test_no_messaging_backend_returns_no_signals(self) -> None:
        self._seed_post(days_old=1.2, last_nag_step=0)
        signals = ReviewNagScanner(messaging=None, user_slack_id="U_ME").scan()
        assert signals == []

    def test_dm_transport_failure_still_marks_done(self) -> None:
        """A raised ``open_dm`` or ``post_message`` on the DM must NOT crash the tick."""
        post = self._seed_post(days_old=6.0, last_nag_step=4)
        slack = FakeSlack(raise_on_open_dm=RuntimeError("slack down"))
        signals = ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()
        post.refresh_from_db()
        # Still marks the row done — we don't keep retrying a broken DM.
        assert post.done_at is not None
        assert [s.kind for s in signals] == ["review_nag.stale_dm"]

    def test_no_user_slack_id_skips_stale_dm_but_still_marks_done(self) -> None:
        """At +5d with no user_slack_id, mark the row done without the DM.

        We can't DM nobody — but we can still stop the nag train.
        """
        post = self._seed_post(days_old=6.0, last_nag_step=4)
        slack = FakeSlack()
        signals = ReviewNagScanner(messaging=slack, user_slack_id="").scan()
        assert slack.posts == []
        post.refresh_from_db()
        assert post.done_at is not None
        assert [s.kind for s in signals] == ["review_nag.stale_no_dm"]

    def test_scanner_name_is_set(self) -> None:
        scanner = ReviewNagScanner(messaging=FakeSlack(), user_slack_id="U_ME")
        assert scanner.name == "review_nag"

    def test_multiple_rows_processed_in_one_scan(self) -> None:
        self._seed_post(
            url="https://gitlab.example/x/-/merge_requests/A",
            thread_ts="ts.A",
            days_old=1.2,
            last_nag_step=0,
        )
        self._seed_post(
            url="https://gitlab.example/x/-/merge_requests/B",
            thread_ts="ts.B",
            days_old=2.2,
            last_nag_step=1,
        )
        slack = FakeSlack()
        signals = ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()
        assert len(slack.posts) == 2
        thread_ts_sent = {p["thread_ts"] for p in slack.posts}
        assert thread_ts_sent == {"ts.A", "ts.B"}
        assert [s.kind for s in signals] == ["review_nag.ping", "review_nag.ping"]

    def test_step_skips_if_already_at_or_past_target(self) -> None:
        """A row at +1.5d with last_nag_step already 2 (somehow) doesn't downgrade."""
        post = self._seed_post(days_old=1.5, last_nag_step=2)
        slack = FakeSlack()
        ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()
        assert slack.posts == []
        post.refresh_from_db()
        assert post.last_nag_step == 2


class TestReviewNagScannerCustomNow(TestCase):
    """Inject a custom ``now`` to test absolute time without flake."""

    def test_now_override_is_respected(self) -> None:
        post = ReviewRequestPost.objects.create(
            mr_url="https://gitlab.example/x/-/merge_requests/99",
            slack_channel_id="C0DEMOCHAN1",
            slack_thread_ts="ts.99",
            created_at=dt.datetime(2026, 5, 1, 12, 0, tzinfo=dt.UTC),
            last_nag_step=0,
        )
        slack = FakeSlack()
        scanner = ReviewNagScanner(
            messaging=slack,
            user_slack_id="U_ME",
            now=dt.datetime(2026, 5, 3, 12, 0, tzinfo=dt.UTC),  # +2 days exactly
        )
        scanner.scan()
        assert len(slack.posts) == 1
        post.refresh_from_db()
        assert post.last_nag_step == 2


class TestNagConsultsDedupGuard(TestCase):
    """Before nagging, the scanner live-reads for an out-of-band post (#1084).

    If the review was requested again / picked up out-of-band, the row is
    reconciled (``done_at`` set, PR transitioned by the guard) and the nag
    is skipped — the train stops. Fails open: no channel/token or a
    failed read means the nag proceeds as before.
    """

    def _due_post(self) -> ReviewRequestPost:
        return ReviewRequestPost.objects.create(
            mr_url="https://gitlab.example/x/-/merge_requests/1",
            slack_channel_id="C0DEMOCHAN1",
            slack_thread_ts="1700000000.001",
            created_at=timezone.now() - dt.timedelta(days=2),
            last_nag_step=0,
        )

    def test_nag_skipped_when_guard_reconciles(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.review_request_guard import GuardTarget  # noqa: PLC0415

        self._due_post()
        slack = FakeSlack()
        target = GuardTarget(channel_id="C0DEMOCHAN1", channel_name="rev", token="xoxb")
        with (
            patch(
                "teatree.core.review_request_guard.resolve_guard_target",
                return_value=target,
            ),
            patch(
                "teatree.core.review_request_guard.reconcile_out_of_band",
                return_value="https://team.slack.com/archives/C/p1",
            ),
        ):
            signals = ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()

        assert slack.posts == []
        assert any(s.kind == "review_nag.reconciled" for s in signals)

    def test_nag_proceeds_when_no_guard_target(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        self._due_post()
        slack = FakeSlack()
        with patch(
            "teatree.core.review_request_guard.resolve_guard_target",
            return_value=None,
        ):
            ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()
        assert len(slack.posts) == 1

    def test_nag_proceeds_when_guard_finds_nothing(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from teatree.core.review_request_guard import GuardTarget  # noqa: PLC0415

        self._due_post()
        slack = FakeSlack()
        target = GuardTarget(channel_id="C0DEMOCHAN1", channel_name="rev", token="xoxb")
        with (
            patch(
                "teatree.core.review_request_guard.resolve_guard_target",
                return_value=target,
            ),
            patch(
                "teatree.core.review_request_guard.reconcile_out_of_band",
                return_value="",
            ),
        ):
            ReviewNagScanner(messaging=slack, user_slack_id="U_ME").scan()
        assert len(slack.posts) == 1
