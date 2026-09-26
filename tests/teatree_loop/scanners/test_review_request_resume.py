"""R3 — a paused, now-ready review request resumes once, in its own thread.

Each case pins one half of the contract the scanner exists to hold: the reply
lands exactly once on the tracked thread, the ``resumed_at`` one-shot survives a
refusal so a later tick can retry, an unready or unreadable request stays held,
and the whole thing is inert until an overlay arms it.

The forge and Slack are the only fakes — the on-behalf gate, the draft gate, the
required-checks classifier and the pause reader all run for real, because each is
an axis that must fail CLOSED and a stub would assert nothing about that.
"""

import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import connections
from django.test import TestCase, override_settings
from django.utils import timezone

from teatree.core.backend_protocols import ROLLUP_QUERY_FAILED, DraftState, PrOpenState
from teatree.core.models import ConfigSetting, ReviewRequestPost
from teatree.core.on_behalf_egress import NO_TOKEN_FOR_DESTINATION
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.review_request_resume import (
    RESUME_REPLY_TEXT,
    ReviewRequestResumeScanner,
    _claim_and_reply,
    _claim_resume,
)
from teatree.on_behalf_gate import OnBehalfContext
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS
from teatree.types import RawAPIDict
from tests.db_alias import RouteAllToAlias, register_sqlite_alias, run_racing_threads, teardown_sqlite_alias
from tests.teatree_core._on_behalf_gate_helpers import posture_forbids_cm, posture_permits_cm

_MR_URL = "https://github.com/o/r/pull/7"
_CHANNEL = "C_REVIEW"
_THREAD_TS = "1780473408.767019"
_PAUSE_EMOJI = "double_vertical_bar"
_REQUIRED_CHECK = "test (3.13)"

_GREEN_ROLLUP: list[RawAPIDict] = [
    {"__typename": "CheckRun", "name": _REQUIRED_CHECK, "status": "COMPLETED", "conclusion": "SUCCESS"},
]
_RED_ROLLUP: list[RawAPIDict] = [
    {"__typename": "CheckRun", "name": _REQUIRED_CHECK, "status": "COMPLETED", "conclusion": "FAILURE"},
]


@dataclass
class _Slack:
    """Route-aware messaging fake — the pause read plus the routed thread reply."""

    reactions: tuple[str, ...] = (_PAUSE_EMOJI,)
    fetch_error: Exception | None = None
    post_error: Exception | None = None
    post_response: RawAPIDict | None = None
    posted: list[tuple[str, str, str]] = field(default_factory=list)

    def route_token(self, channel: str) -> str:
        _ = channel
        return "xoxp-user"

    def _is_self_dm(self, channel: str) -> bool:
        _ = channel
        return False

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        _ = channel
        if self.fetch_error is not None:
            raise self.fetch_error
        return {"ts": ts, "reactions": [{"name": name} for name in self.reactions]}

    def post_routed(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        if self.post_error is not None:
            raise self.post_error
        self.posted.append((channel, text, thread_ts))
        return {"ok": True} if self.post_response is None else self.post_response

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""


@dataclass
class _Host:
    """Forge fake answering the two readiness axes off the injected code host."""

    draft: DraftState = DraftState.NOT_DRAFT
    rollup: list[RawAPIDict] = field(default_factory=lambda: list(_GREEN_ROLLUP))
    rollup_error: Exception | None = None
    current: str = "owner"
    author: str = "owner"
    author_error: Exception | None = None
    open_state: PrOpenState = PrOpenState.OPEN
    open_state_error: Exception | None = None

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        _ = pr_url
        if self.open_state_error is not None:
            raise self.open_state_error
        return self.open_state

    def fetch_pr_draft_state(self, *, slug: str, pr_id: int) -> DraftState:
        _ = (slug, pr_id)
        return self.draft

    def fetch_required_checks_rollup(self, *, slug: str, pr_id: int) -> list[RawAPIDict]:
        _ = (slug, pr_id)
        if self.rollup_error is not None:
            raise self.rollup_error
        return list(self.rollup)

    def fetch_required_status_check_contexts(self, *, slug: str, pr_id: int) -> list[RawAPIDict]:
        _ = (slug, pr_id)
        return [{"context": _REQUIRED_CHECK}]

    def current_user(self) -> str:
        return self.current

    def get_pr_author(self, *, pr_url: str) -> str:
        _ = pr_url
        if self.author_error is not None:
            raise self.author_error
        return self.author


def _seed(
    *,
    mr_url: str = _MR_URL,
    overlay: str | None = "overlay-a",
) -> ReviewRequestPost:
    return ReviewRequestPost.objects.create(
        mr_url=mr_url,
        overlay=overlay,
        slack_channel_id=_CHANNEL,
        slack_thread_ts=_THREAD_TS,
        created_at=timezone.now(),
    )


def _kinds(signals: list[ScanSignal]) -> list[str]:
    return [signal.kind for signal in signals]


class TestReviewRequestResumeScanner(TestCase):
    """One armed scanner over one paused row — the full readiness / refusal matrix."""

    def setUp(self) -> None:
        super().setUp()
        ConfigSetting.objects.set_value("review_resume_reply_enabled", value=True)
        self.enterContext(posture_permits_cm())
        self.post = _seed()

    def _scan(self, slack: _Slack, host: _Host) -> list[ScanSignal]:
        with mock.patch("teatree.core.backend_factory.code_host_from_overlay", return_value=host):
            return ReviewRequestResumeScanner(
                messaging=slack,
                host=host,
                overlay="overlay-a",
                identities=("owner",),
            ).scan()

    def test_other_overlay_and_unattributed_rows_are_skipped(self) -> None:
        _seed(mr_url="https://github.com/o/r/pull/8", overlay=None)
        slack = _Slack()

        signals = ReviewRequestResumeScanner(
            messaging=slack,
            host=_Host(),
            overlay="overlay-b",
        ).scan()

        assert signals == []
        assert slack.posted == []

    def test_paused_and_ready_replies_once_in_the_tracked_thread(self) -> None:
        slack = _Slack()

        signals = self._scan(slack, _Host())

        assert _kinds(signals) == ["review_request.resumed"]
        assert slack.posted == [(_CHANNEL, RESUME_REPLY_TEXT, _THREAD_TS)]
        self.post.refresh_from_db()
        assert self.post.resumed_at is not None

    def test_colleague_authored_request_refuses_before_resume_claim(self) -> None:
        slack = _Slack()
        with mock.patch("teatree.loop.scanners.review_request_resume._claim_resume") as claim:
            signals = self._scan(slack, _Host(author="colleague"))

        claim.assert_not_called()
        self.post.refresh_from_db()
        assert self.post.resumed_at is None
        assert slack.posted == []
        assert _kinds(signals) == ["review_request.foreign_author"]

    def test_unknown_authorship_refuses_before_resume_claim(self) -> None:
        cases = (
            (_Host(author=""), ("owner",)),
            (_Host(author_error=RuntimeError("down")), ("owner",)),
            (None, ("owner",)),
        )
        for index, (host, identities) in enumerate(cases):
            slack = _Slack()
            with (
                self.subTest(index=index),
                mock.patch("teatree.loop.scanners.review_request_resume._claim_resume") as claim,
            ):
                signals = ReviewRequestResumeScanner(
                    messaging=slack,
                    host=host,
                    overlay="overlay-a",
                    identities=identities,
                ).scan()
                claim.assert_not_called()
                assert slack.posted == []
                assert _kinds(signals) == ["review_request.authorship_unreadable"]

    def test_a_second_tick_posts_nothing_more(self) -> None:
        slack = _Slack()

        self._scan(slack, _Host())
        second = self._scan(slack, _Host())

        assert second == []
        assert len(slack.posted) == 1

    def test_a_gated_post_releases_the_claim(self) -> None:
        slack = _Slack()

        with posture_forbids_cm():
            signals = self._scan(slack, _Host())

        assert _kinds(signals) == ["review_request.resume_gated"]
        assert slack.posted == []
        self.post.refresh_from_db()
        assert self.post.resumed_at is None

    def test_a_failed_transport_releases_the_claim(self) -> None:
        slack = _Slack(post_error=RuntimeError("slack 503"))

        signals = self._scan(slack, _Host())

        assert _kinds(signals) == ["review_request.resume_failed"]
        self.post.refresh_from_db()
        assert self.post.resumed_at is None

    def test_a_publish_slack_refused_releases_the_claim_and_retries(self) -> None:
        # The egress hands an unlanded publish back WITHOUT raising, so discarding the
        # body burns the single-use one-shot on a reply nobody ever saw.
        refused = _Slack(post_response={"ok": False, "error": "not_in_channel"})

        signals = self._scan(refused, _Host())

        assert _kinds(signals) == ["review_request.resume_failed"]
        assert signals[0].payload["error"] == "not_in_channel"
        self.post.refresh_from_db()
        assert self.post.resumed_at is None

        recovered = _Slack()
        assert _kinds(self._scan(recovered, _Host())) == ["review_request.resumed"]
        assert recovered.posted == [(_CHANNEL, RESUME_REPLY_TEXT, _THREAD_TS)]

    def test_an_empty_publish_body_releases_the_claim(self) -> None:
        signals = self._scan(_Slack(post_response={}), _Host())

        assert _kinds(signals) == ["review_request.resume_failed"]
        assert signals[0].payload["error"] == NO_TOKEN_FOR_DESTINATION["error"]
        self.post.refresh_from_db()
        assert self.post.resumed_at is None

    def test_a_row_retired_between_the_read_and_the_claim_is_never_posted(self) -> None:
        # The queryset filters on done_at, but the claim did not — so a merge-react
        # retiring the row in that window still got "Now ready for review." posted.
        slack = _Slack()
        real_claim = _claim_resume

        def retire_then_claim(post: ReviewRequestPost, claimed_at: object) -> bool:
            ReviewRequestPost.objects.filter(pk=post.pk).update(done_at=timezone.now())
            return real_claim(post, claimed_at)

        with mock.patch("teatree.loop.scanners.review_request_resume._claim_resume", side_effect=retire_then_claim):
            signals = self._scan(slack, _Host())

        assert signals == []
        assert slack.posted == []

    def test_an_open_gitlab_merge_request_resumes(self) -> None:
        # GitLab upper-cases its own `opened` to OPENED, so comparing merge state to OPEN held every one.
        slack = _Slack()

        assert _kinds(self._scan(slack, _Host(open_state=PrOpenState.OPEN))) == ["review_request.resumed"]

    def test_a_merged_closed_or_unreadable_mr_is_never_told_it_is_ready(self) -> None:
        for state in (PrOpenState.MERGED, PrOpenState.CLOSED, PrOpenState.UNKNOWN):
            with self.subTest(state=state.value):
                slack = _Slack()
                assert self._scan(slack, _Host(open_state=state)) == []
                assert slack.posted == []
                self.post.refresh_from_db()
                assert self.post.resumed_at is None

    def test_an_unreadable_open_state_holds_the_resume(self) -> None:
        slack = _Slack()

        assert self._scan(slack, _Host(open_state_error=RuntimeError("forge down"))) == []
        assert slack.posted == []

    def test_a_released_claim_lets_a_later_tick_retry(self) -> None:
        failing = _Slack(post_error=RuntimeError("slack 503"))
        self._scan(failing, _Host())

        recovered = _Slack()
        signals = self._scan(recovered, _Host())

        assert _kinds(signals) == ["review_request.resumed"]
        assert recovered.posted == [(_CHANNEL, RESUME_REPLY_TEXT, _THREAD_TS)]

    def test_a_review_exempt_repo_is_never_resumed(self) -> None:
        """The reply IS a review request on a colleague's thread — the exemption governs it.

        Its sibling above is the control: same fixture, same armed scanner, the
        pinned pattern the only difference.
        """
        ConfigSetting.objects.set_value("review_exempt_repos", ["o/r"])
        slack = _Slack()

        signals = self._scan(slack, _Host())

        assert signals == []
        assert slack.posted == []
        self.post.refresh_from_db()
        assert self.post.resumed_at is None

    def test_a_draft_merge_request_is_not_resumed(self) -> None:
        slack = _Slack()

        signals = self._scan(slack, _Host(draft=DraftState.DRAFT))

        assert signals == []
        assert slack.posted == []
        self.post.refresh_from_db()
        assert self.post.resumed_at is None

    def test_red_required_checks_are_not_resumed(self) -> None:
        slack = _Slack()

        signals = self._scan(slack, _Host(rollup=list(_RED_ROLLUP)))

        assert signals == []
        assert slack.posted == []

    def test_an_unreadable_rollup_holds_the_resume(self) -> None:
        slack = _Slack()

        signals = self._scan(slack, _Host(rollup_error=RuntimeError("gh 502")))

        assert signals == []
        assert slack.posted == []
        self.post.refresh_from_db()
        assert self.post.resumed_at is None

    def test_a_sentinel_rollup_classified_unreadable_holds_the_resume(self) -> None:
        """The read SUCCEEDS and reports "could not be read" — the new verdict, held.

        Distinct from the sibling above, which raises: here the classifier really
        returns ``"unreadable"``, and the resume gate holds because it tests for
        GREEN rather than listing the reds it knows about.
        """
        slack = _Slack()

        signals = self._scan(slack, _Host(rollup=[ROLLUP_QUERY_FAILED]))

        assert signals == []
        assert slack.posted == []
        self.post.refresh_from_db()
        assert self.post.resumed_at is None

    def test_an_unreadable_pause_is_surfaced_and_posts_nothing(self) -> None:
        slack = _Slack(fetch_error=RuntimeError("slack 500"))

        signals = self._scan(slack, _Host())

        assert _kinds(signals) == ["review_request.pause_unreadable"]
        assert slack.posted == []
        self.post.refresh_from_db()
        assert self.post.resumed_at is None

    def test_an_unpaused_request_is_left_alone(self) -> None:
        slack = _Slack(reactions=("eyes",))

        signals = self._scan(slack, _Host())

        assert signals == []
        assert slack.posted == []

    def test_a_scanner_without_a_code_host_reports_unreadable_authorship(self) -> None:
        slack = _Slack()

        signals = ReviewRequestResumeScanner(messaging=slack, host=None, overlay="overlay-a").scan()

        assert [signal.kind for signal in signals] == ["review_request.authorship_unreadable"]
        assert slack.posted == []


class TestLosingTickPostsNothing(TestCase):
    """The tick that loses the claim stands down — no second message on the thread.

    Both instances are loaded while the row is still unclaimed, which is exactly
    what two ticks hold when they overlap: the claim, not either instance's stale
    ``resumed_at``, decides who posts.

    Anti-vacuity: replace the conditional claim in ``_claim_resume`` with a
    read-modify-write over the instance (``if post.resumed_at is None: post.resumed_at
    = claimed_at; post.save()``) and this goes RED — the loser reads its own stale
    ``None`` and posts a duplicate reply.
    """

    def test_the_second_tick_neither_claims_nor_posts(self) -> None:
        self.enterContext(posture_permits_cm())
        _seed()
        first, second = ReviewRequestPost.objects.all()[0], ReviewRequestPost.objects.all()[0]
        slack = _Slack()

        context = OnBehalfContext(own_mr=True, target=_MR_URL)
        winner = _claim_and_reply(first, slack, context=context)
        loser = _claim_and_reply(second, slack, context=context)

        assert winner is not None
        assert winner.kind == "review_request.resumed"
        assert loser is None
        assert len(slack.posted) == 1, f"expected exactly one thread reply, got {slack.posted!r}"


class _PinReviewRequestPost:
    """Route every ``ReviewRequestPost`` query to the private file-backed alias.

    The production claim takes no ``using``, so the router is what puts it on a
    database two real threads can both reach: the xdist worker's ``:memory:``
    database is private to the one connection the session fixture restored it
    into, and a second thread opening it finds an empty schema — the lost update
    could not even be staged there.
    """

    def __init__(self, alias: str) -> None:
        self.alias = alias

    def _route(self, model: type[object]) -> str | None:
        return self.alias if model is ReviewRequestPost else None

    def db_for_read(self, model: type[object], **hints: object) -> str | None:
        return self._route(model)

    def db_for_write(self, model: type[object], **hints: object) -> str | None:
        return self._route(model)


def _make_alias(tmp_path: Path) -> str:
    """A fresh file-backed connection carrying prod's write-serialization options.

    ``transaction_mode="IMMEDIATE"`` is prod's (``SQLITE_WRITE_SERIALIZATION_OPTIONS``),
    so the second writer meets the same reserved write lock it meets in production
    rather than a laxer local default. The real migration graph creates the
    schema so this fixture cannot drift from ``ReviewRequestPost``.
    """
    alias = f"resume_{uuid.uuid4().hex}"
    db_file = tmp_path / f"{alias}.sqlite3"
    register_sqlite_alias(alias, db_file, options=SQLITE_WRITE_SERIALIZATION_OPTIONS)
    with override_settings(DATABASE_ROUTERS=[RouteAllToAlias(alias)]):
        call_command("migrate", "--no-input", database=alias, verbosity=0)
    connections[alias].close()
    return alias


def _race_the_claim(post_pk: int) -> list[bool]:
    """Two real threads, two connections, one conditional UPDATE each.

    The read is deliberately BEFORE the barrier: both ticks hold a row snapshot
    taken while ``resumed_at`` was still null, which is the stale state the
    conditional UPDATE has to arbitrate.
    """
    barrier = threading.Barrier(2)

    def tick(_index: int) -> bool:
        post = ReviewRequestPost.objects.get(pk=post_pk)
        barrier.wait(timeout=10)
        return _claim_resume(post, timezone.now())

    return run_racing_threads(tick, 2)


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    """Lift pytest-django's DB guard — this module registers and tears down its own alias."""
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_unblocked_db")
class TestConcurrentResumeClaim:
    """Two concurrent ticks over one row hand the resume to exactly ONE of them.

    Real threads on real connections against prod's write-serialization options,
    mirroring ``tests/teatree_core/test_review_request_post_concurrent.py``: the
    lost update this pins does not exist within a single connection.

    Anti-vacuity: swap ``_claim_resume``'s conditional UPDATE for a bare-autocommit
    read-modify-write over the loaded instance (``if post.resumed_at is None:
    post.resumed_at = claimed_at; post.save()``) and both threads claim — two
    replies on one thread. No ``atomic()`` around it, because the lost update this
    pins lives in exactly that unlocked shape.
    """

    def test_exactly_one_tick_wins_the_claim(self, tmp_path: Path) -> None:
        alias = _make_alias(tmp_path)
        try:
            with override_settings(DATABASE_ROUTERS=[_PinReviewRequestPost(alias)]):
                post = ReviewRequestPost.objects.create(
                    mr_url=_MR_URL,
                    slack_channel_id=_CHANNEL,
                    slack_thread_ts=_THREAD_TS,
                    created_at=timezone.now(),
                )
                outcomes = _race_the_claim(post.pk)
                post.refresh_from_db()
                resumed_at = post.resumed_at
        finally:
            teardown_sqlite_alias(alias)

        assert outcomes.count(True) == 1, f"expected exactly one winner, got {outcomes!r}"
        assert outcomes.count(False) == 1, f"expected exactly one tick to stand down, got {outcomes!r}"
        assert resumed_at is not None


class TestTheResumeReplyShipsOff(TestCase):
    """A paused request waits silently until ``review_resume_reply_enabled`` opts the box in."""

    def test_an_armed_row_posts_nothing_by_default(self) -> None:
        self.enterContext(posture_permits_cm())
        _seed()
        slack = _Slack()

        signals = ReviewRequestResumeScanner(messaging=slack, host=_Host(), overlay="overlay-a").scan()

        assert signals == []
        assert slack.posted == []
