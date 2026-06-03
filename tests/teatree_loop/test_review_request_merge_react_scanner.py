"""Tests for the ReviewRequestMergeReactScanner (#1797).

When a review-request the agent posted to Slack later *merges*, the loop
adds a single ``:merge:`` reaction to the original review-request message
so reviewers see at a glance that the request landed.

The scanner walks open ``ReviewRequestPost`` rows, checks each MR's
open-state through the code host, and on ``MERGED`` reacts on the tracked
``(slack_channel_id, slack_thread_ts)`` and marks the row done so it
reacts exactly once. The reaction goes out under the #1750 routing
(``react_routed``: colleague/channel → ``xoxp``, self-DM → bot); a
missing ``reactions:write`` scope degrades to a surfaced signal, never a
crash.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase
from django.utils import timezone

from teatree.backends.protocols import PrOpenState
from teatree.core.models import ReviewRequestPost
from teatree.loop.scanners.review_request_merge_react import MERGE_REACTION_EMOJI, ReviewRequestMergeReactScanner
from teatree.types import RawAPIDict


@dataclass
class FakeSlack:
    """In-memory MessagingBackend recording ``react_routed`` calls."""

    reactions: list[dict[str, Any]] = field(default_factory=list)
    react_result: RawAPIDict | None = None
    raise_on_react: Exception | None = None

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        if self.raise_on_react is not None:
            raise self.raise_on_react
        self.reactions.append({"channel": channel, "ts": ts, "emoji": emoji})
        return {"ok": True} if self.react_result is None else self.react_result


@dataclass
class FakeHost:
    """In-memory ``CodeHostBackend`` returning a fixed open-state per URL."""

    open_state: PrOpenState | None = None
    states_by_url: dict[str, PrOpenState] = field(default_factory=dict)
    raise_on_lookup: Exception | None = None
    lookups: list[str] = field(default_factory=list)
    user: str = ""
    author: str = ""
    authors_by_url: dict[str, str] = field(default_factory=dict)
    raise_on_author: Exception | None = None

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        self.lookups.append(pr_url)
        if self.raise_on_lookup is not None:
            raise self.raise_on_lookup
        if pr_url in self.states_by_url:
            return self.states_by_url[pr_url]
        assert self.open_state is not None
        return self.open_state

    def current_user(self) -> str:
        return self.user

    def get_pr_author(self, *, pr_url: str) -> str:
        if self.raise_on_author is not None:
            raise self.raise_on_author
        return self.authors_by_url.get(pr_url, self.author)


class _SeedMixin:
    def _seed_post(self, **overrides: Any) -> ReviewRequestPost:
        spec: dict[str, Any] = {
            "url": "https://github.com/o/r/pull/1",
            "channel": "C_REVIEW",
            "thread_ts": "1700000000.001",
            "days_old": 0.1,
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


class TestMergeReaction(_SeedMixin, TestCase):
    def test_merged_request_gets_exactly_one_merge_reaction(self) -> None:
        post = self._seed_post()
        slack = FakeSlack()
        host = FakeHost(open_state=PrOpenState.MERGED)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host)

        signals = scanner.scan()

        assert slack.reactions == [
            {"channel": "C_REVIEW", "ts": "1700000000.001", "emoji": MERGE_REACTION_EMOJI},
        ]
        post.refresh_from_db()
        assert post.done_at is not None
        assert [s.kind for s in signals] == ["review_request_merge_react.reacted"]

    def test_emoji_is_merge(self) -> None:
        assert MERGE_REACTION_EMOJI == "merge"

    def test_second_scan_does_not_double_react(self) -> None:
        self._seed_post()
        slack = FakeSlack()
        host = FakeHost(open_state=PrOpenState.MERGED)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host)

        scanner.scan()
        signals = scanner.scan()

        assert len(slack.reactions) == 1
        assert signals == []

    def test_open_request_is_not_reacted(self) -> None:
        post = self._seed_post()
        slack = FakeSlack()
        host = FakeHost(open_state=PrOpenState.OPEN)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host)

        signals = scanner.scan()

        assert slack.reactions == []
        post.refresh_from_db()
        assert post.done_at is None
        assert signals == []

    def test_closed_request_is_not_reacted_but_left_for_nag_to_resolve(self) -> None:
        post = self._seed_post()
        slack = FakeSlack()
        host = FakeHost(open_state=PrOpenState.CLOSED)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host)

        signals = scanner.scan()

        assert slack.reactions == []
        post.refresh_from_db()
        assert post.done_at is None
        assert signals == []

    def test_already_done_row_is_skipped(self) -> None:
        self._seed_post(done_at=timezone.now())
        slack = FakeSlack()
        host = FakeHost(open_state=PrOpenState.MERGED)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host)

        signals = scanner.scan()

        assert slack.reactions == []
        assert signals == []

    def test_only_merged_rows_are_reacted_among_many(self) -> None:
        self._seed_post(url="https://github.com/o/r/pull/1", thread_ts="ts.open")
        self._seed_post(url="https://github.com/o/r/pull/2", thread_ts="ts.merged")
        slack = FakeSlack()
        host = FakeHost(
            open_state=PrOpenState.OPEN,
            states_by_url={"https://github.com/o/r/pull/2": PrOpenState.MERGED},
        )
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host)

        signals = scanner.scan()

        assert [r["ts"] for r in slack.reactions] == ["ts.merged"]
        assert [s.kind for s in signals] == ["review_request_merge_react.reacted"]


class TestConcurrentTickReactsExactlyOnce(_SeedMixin, TestCase):
    """Two concurrent ticks against the same merged row react EXACTLY once.

    Both ticks see the row as merged and not-yet-done; the atomic
    ``done_at`` claim lets exactly one win the ``UPDATE`` and react, the
    loser matches zero rows and skips. Revert the claim to a blind save and
    this would react twice.
    """

    def test_two_ticks_same_row_react_once(self) -> None:
        self._seed_post(thread_ts="ts.race")
        host = FakeHost(open_state=PrOpenState.MERGED)
        scanner = ReviewRequestMergeReactScanner(messaging=FakeSlack(), host=host)

        post_tick_a = ReviewRequestPost.objects.get(slack_thread_ts="ts.race")
        post_tick_b = ReviewRequestPost.objects.get(slack_thread_ts="ts.race")

        slack = FakeSlack()
        signal_a = scanner._process_one(post_tick_a, slack, host)
        signal_b = scanner._process_one(post_tick_b, slack, host)

        assert len(slack.reactions) == 1
        kinds = [s.kind for s in (signal_a, signal_b) if s is not None]
        assert kinds == ["review_request_merge_react.reacted"]


class TestGracefulDegradation(_SeedMixin, TestCase):
    def test_missing_reactions_scope_surfaces_signal_and_marks_done(self) -> None:
        post = self._seed_post()
        slack = FakeSlack(
            react_result={"ok": False, "error": "missing_scope", "needed": "reactions:write"},
        )
        host = FakeHost(open_state=PrOpenState.MERGED)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host)

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["review_request_merge_react.missing_scope"]
        assert "reactions:write" in signals[0].summary
        # The MR really did merge — the row is closed so the nag train stops
        # and the scanner never retries a reaction it can never land.
        post.refresh_from_db()
        assert post.done_at is not None

    def test_already_reacted_is_treated_as_success(self) -> None:
        post = self._seed_post()
        slack = FakeSlack(react_result={"ok": False, "error": "already_reacted"})
        host = FakeHost(open_state=PrOpenState.MERGED)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host)

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["review_request_merge_react.reacted"]
        post.refresh_from_db()
        assert post.done_at is not None

    def test_transient_react_error_leaves_row_open_for_retry(self) -> None:
        post = self._seed_post()
        slack = FakeSlack(raise_on_react=RuntimeError("not_in_channel"))
        host = FakeHost(open_state=PrOpenState.MERGED)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host)

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["review_request_merge_react.react_failed"]
        post.refresh_from_db()
        assert post.done_at is None

    def test_react_returns_other_api_error_leaves_row_open(self) -> None:
        post = self._seed_post()
        slack = FakeSlack(react_result={"ok": False, "error": "channel_not_found"})
        host = FakeHost(open_state=PrOpenState.MERGED)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host)

        signals = scanner.scan()

        assert [s.kind for s in signals] == ["review_request_merge_react.react_failed"]
        post.refresh_from_db()
        assert post.done_at is None

    def test_open_state_lookup_failure_skips_row(self) -> None:
        post = self._seed_post()
        slack = FakeSlack()
        host = FakeHost(raise_on_lookup=RuntimeError("github 500"))
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host)

        signals = scanner.scan()

        assert slack.reactions == []
        post.refresh_from_db()
        assert post.done_at is None
        assert signals == []

    def test_unknown_open_state_skips_row(self) -> None:
        post = self._seed_post()
        slack = FakeSlack()
        host = FakeHost(open_state=PrOpenState.UNKNOWN)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host)

        signals = scanner.scan()

        assert slack.reactions == []
        post.refresh_from_db()
        assert post.done_at is None
        assert signals == []


class TestDegenerateConfig(_SeedMixin, TestCase):
    def test_no_messaging_backend_is_a_noop(self) -> None:
        self._seed_post()
        host = FakeHost(open_state=PrOpenState.MERGED)
        scanner = ReviewRequestMergeReactScanner(messaging=None, host=host)

        signals = scanner.scan()

        assert signals == []

    def test_no_host_is_a_noop(self) -> None:
        self._seed_post()
        slack = FakeSlack()
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=None)

        signals = scanner.scan()

        assert slack.reactions == []
        assert signals == []

    def test_row_without_thread_ts_is_skipped(self) -> None:
        post = self._seed_post(thread_ts="")
        slack = FakeSlack()
        host = FakeHost(open_state=PrOpenState.MERGED)
        scanner = ReviewRequestMergeReactScanner(messaging=slack, host=host)

        signals = scanner.scan()

        assert slack.reactions == []
        post.refresh_from_db()
        assert post.done_at is None
        assert signals == []

    def test_scanner_name_is_set(self) -> None:
        scanner = ReviewRequestMergeReactScanner(messaging=FakeSlack(), host=FakeHost())
        assert scanner.name == "review_request_merge_react"
