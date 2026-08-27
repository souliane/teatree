"""The MR-triage surveyor: it decides, it surfaces, and it never acts.

The scanner's whole job is to run the operator's own open MRs through the pure
ladder and say what each one needs. It has no post path and no dispatch, so the
cases below pin what it READS (the facts it can honestly build) and what it
SURFACES — never a side effect, because the actions the ladder names are
colleague-visible and belong to the owner.
"""

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.gates.review_request_guard import GuardTarget
from teatree.core.models import DeferredQuestion, ReviewRequestPost
from teatree.core.review.mr_state_question import mr_state_marker
from teatree.core.review.mr_triage import RepoOwner, TriageAction, TriageReason
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.mr_triage_scan import MrTriageScanner
from tests.teatree_loop.test_scanners import FakeCodeHost

_REPO = "https://gitlab.example.com/group/repo/-/merge_requests"
_OTHER_REPO = "https://gitlab.example.com/other/repo/-/merge_requests"


def _mr(iid: int, **payload: object) -> dict[str, object]:
    return {"iid": iid, "title": f"MR {iid}", "web_url": f"{_REPO}/{iid}", "sha": f"{iid:040d}", **payload}


def _green(iid: int, **payload: object) -> dict[str, object]:
    return _mr(iid, head_pipeline={"status": "success"}, **payload)


def _opened(iid: int, *, days_ago: float = 2.0, **payload: object) -> dict[str, object]:
    """A green merge request carrying the creation stamp the channel read is measured against."""
    return _green(iid, created_at=(timezone.now() - dt.timedelta(days=days_ago)).isoformat(), **payload)


@contextmanager
def _channel() -> Iterator[None]:
    target = GuardTarget(channel_id="C1", channel_name="reviews", token="xoxb-bot")
    with patch("teatree.loop.scanners.mr_triage_scan.resolve_guard_target", return_value=target):
        yield


@contextmanager
def _reads(*, ok: bool = True, asked: tuple[str, ...] = ()) -> Iterator[None]:
    """Stage the review channel's history. ``ok=False`` is a FAILED read, never an empty one."""
    now = timezone.now().timestamp()
    matches = [type("M", (), {"pr_url": url, "ts": f"{now:.6f}"})() for url in asked]
    read = type("R", (), {"ok": ok, "matches": matches})
    provider = type("P", (), {"read_recent_review_matches": lambda _spec: read()})
    with patch("teatree.core.backend_registry.get_backend_provider", return_value=provider):
        yield


@contextmanager
def _exempt(*patterns: str) -> Iterator[None]:
    with patch("teatree.loop.scanners.mr_triage_scan.review_exempt_patterns", return_value=patterns):
        yield


def _open_mr_questions() -> list[DeferredQuestion]:
    return list(DeferredQuestion.pending().filter(dedupe_marker__startswith="mr-state:"))


def _grouped(iid: int, *, repo: str = _REPO, **payload: object) -> dict[str, object]:
    """A merge request whose title shares a ticket reference with its siblings."""
    return {
        "iid": iid,
        "title": f"feat: part {iid} (repo#42)",
        "web_url": f"{repo}/{iid}",
        "sha": f"{iid:040d}",
        **payload,
    }


def _seed_request(iid: int, *, days_idle: float) -> ReviewRequestPost:
    return ReviewRequestPost.objects.create(
        mr_url=f"{_REPO}/{iid}",
        slack_channel_id="C1",
        slack_thread_ts=f"ts.{iid}",
        created_at=timezone.now() - dt.timedelta(days=days_idle),
    )


def _actions(signals: list[ScanSignal]) -> list[object]:
    return [s.payload["action"] for s in signals]


class TestItSurfacesAVerdictPerMr(TestCase):
    def test_a_red_mr_asks_for_the_ci_fix(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_mr(1, head_pipeline={"status": "failed"})])

        signals = MrTriageScanner(host=host).scan()

        assert _actions(signals) == [TriageAction.FIX_CI]

    def test_a_green_mr_with_no_ledger_row_is_an_owner_question_not_a_review_request(self) -> None:
        """No row does not prove nobody was asked, so the ladder must not act on it."""
        host = FakeCodeHost(user="alice", my_prs=[_green(2)])

        signals = MrTriageScanner(host=host).scan()

        assert _actions(signals) == [TriageAction.ASK_OWNER]

    def test_a_requested_mr_past_its_window_names_the_group_ping(self) -> None:
        _seed_request(3, days_idle=3.0)
        host = FakeCodeHost(user="alice", my_prs=[_green(3)])

        signals = MrTriageScanner(host=host, repo_owner=lambda _slug: RepoOwner.ENGINEERING).scan()

        assert _actions(signals) == [TriageAction.GROUP_PING]

    def test_the_same_mr_on_a_devops_repo_is_still_waiting(self) -> None:
        _seed_request(4, days_idle=3.0)
        host = FakeCodeHost(user="alice", my_prs=[_green(4)])

        signals = MrTriageScanner(host=host, repo_owner=lambda _slug: RepoOwner.DEVOPS).scan()

        assert _actions(signals) == [TriageAction.WAIT]

    def test_an_approved_mr_needs_nothing(self) -> None:
        _seed_request(5, days_idle=30.0)
        host = FakeCodeHost(user="alice", my_prs=[_green(5)], approvals={"approved_by": [{"user": {"username": "bo"}}]})

        signals = MrTriageScanner(host=host).scan()

        assert signals == []

    def test_a_draft_leaves_triage_entirely(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_green(6, draft=True)])

        assert MrTriageScanner(host=host).scan() == []


class TestWhatItCannotKnowBecomesAQuestion(TestCase):
    def test_an_mr_with_no_review_request_row_and_no_ci_is_an_owner_question(self) -> None:
        """Neither signal is readable, so the ladder's fallback is the honest answer."""
        host = FakeCodeHost(user="alice", my_prs=[_mr(7)])

        signals = MrTriageScanner(host=host).scan()

        assert _actions(signals) == [TriageAction.ASK_OWNER]

    def test_an_unreadable_approval_probe_does_not_claim_the_mr_is_unapproved(self) -> None:
        _seed_request(8, days_idle=30.0)
        host = FakeCodeHost(user="alice", my_prs=[_green(8)], raise_on_approvals=RuntimeError("forge down"))

        signals = MrTriageScanner(host=host).scan()

        assert _actions(signals) == [TriageAction.ASK_OWNER]


class TestItIsBoundedAndScoped(TestCase):
    def test_it_stops_at_the_per_tick_cap(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_green(n) for n in range(10)])

        signals = MrTriageScanner(host=host, max_mrs_per_tick=3).scan()

        assert len(signals) == 3

    def test_an_mr_outside_the_overlays_url_claim_is_skipped(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_green(9)])

        signals = MrTriageScanner(host=host, allowed_url_prefixes=("https://gitlab.example.com/other/",)).scan()

        assert signals == []


class TestAWorkGroupIsHeldUntilEveryMemberIsReady(TestCase):
    def test_a_red_sibling_holds_the_green_member(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                _grouped(20, head_pipeline={"status": "success"}),
                _grouped(21, head_pipeline={"status": "failed"}),
            ],
        )

        held = [s for s in MrTriageScanner(host=host).scan() if s.payload["url"] == f"{_REPO}/20"]

        assert [s.payload["action"] for s in held] == [TriageAction.WAIT]
        assert held[0].payload["reason"] is TriageReason.WORK_GROUP_NOT_READY
        assert held[0].payload["detail"] == f"{_REPO}/20"

    def test_a_draft_sibling_holds_the_group_it_belongs_to(self) -> None:
        """The draft leaves triage on its own account; its group still waits for it."""
        host = FakeCodeHost(
            user="alice",
            my_prs=[_grouped(22, head_pipeline={"status": "success"}), _grouped(23, draft=True)],
        )

        signals = MrTriageScanner(host=host).scan()

        assert _actions(signals) == [TriageAction.WAIT]

    def test_a_group_whose_members_are_all_green_is_not_held(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                _grouped(24, head_pipeline={"status": "success"}),
                _grouped(25, head_pipeline={"status": "success"}),
            ],
        )

        signals = MrTriageScanner(host=host).scan()

        assert _actions(signals) == [TriageAction.ASK_OWNER, TriageAction.ASK_OWNER]

    def test_a_merge_request_that_shares_no_signal_is_never_held_by_its_own_group(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_green(26), _grouped(27, head_pipeline={"status": "failed"})])

        surfaced = [s for s in MrTriageScanner(host=host).scan() if s.payload["url"] == f"{_REPO}/26"]

        assert [s.payload["action"] for s in surfaced] == [TriageAction.ASK_OWNER]
        assert surfaced[0].payload["detail"] == ""

    def test_the_group_is_read_from_the_unfiltered_listing_so_it_spans_repos(self) -> None:
        """A url-prefixed listing makes a cross-repo group look smaller than it is.

        The sibling here is outside the overlay's url claim, so it is never
        surfaced — but it is still the same unit of work, and it still holds.
        """
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                _grouped(28, head_pipeline={"status": "success"}),
                _grouped(29, repo=_OTHER_REPO, head_pipeline={"status": "failed"}),
            ],
        )

        signals = MrTriageScanner(host=host, allowed_url_prefixes=(f"{_REPO}/",)).scan()

        assert [s.payload["url"] for s in signals] == [f"{_REPO}/28"]
        assert signals[0].payload["reason"] is TriageReason.WORK_GROUP_NOT_READY


class TestAMissingReviewIsProvedAgainstTheChannel(TestCase):
    """The one fact no ledger can supply: nobody has been asked yet.

    An absent ledger row is silence, so the surveyor reads the review channel
    itself. What it earns there is the ladder's review-request rung — the first
    time a merge request nobody ever asked about becomes visible as such.
    """

    def test_a_green_mr_the_channel_never_carried_is_a_review_request(self) -> None:
        with _channel(), _reads():
            signals = MrTriageScanner(host=FakeCodeHost(user="alice", my_prs=[_opened(30)])).scan()

        assert _actions(signals) == [TriageAction.REQUEST_REVIEW]

    def test_an_ask_already_in_the_channel_is_not_a_second_request(self) -> None:
        """Asked out of band, so the ledger holds no clock — it waits rather than ask again."""
        with _channel(), _reads(asked=(f"{_REPO}/31",)):
            signals = MrTriageScanner(host=FakeCodeHost(user="alice", my_prs=[_opened(31)])).scan()

        assert _actions(signals) == [TriageAction.WAIT]
        assert _open_mr_questions() == []

    def test_a_failed_channel_read_is_never_a_missing_review(self) -> None:
        with _channel(), _reads(ok=False):
            signals = MrTriageScanner(host=FakeCodeHost(user="alice", my_prs=[_opened(32)])).scan()

        assert _actions(signals) == [TriageAction.ASK_OWNER]

    def test_the_missing_review_reaches_the_owner_not_only_the_statusline(self) -> None:
        with _channel(), _reads():
            MrTriageScanner(host=FakeCodeHost(user="alice", my_prs=[_opened(33)])).scan()

        assert [q.dedupe_marker for q in _open_mr_questions()] == [mr_state_marker(f"{_REPO}/33")]

    def test_a_held_group_member_asks_the_owner_nothing(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[_opened(34, title="feat: part a (repo#7)"), _opened(35, title="feat: part b (repo#7)", draft=True)],
        )

        with _channel(), _reads():
            MrTriageScanner(host=host).scan()

        assert _open_mr_questions() == []


class TestAReviewExemptRepoIsNeverAskedAbout(TestCase):
    """R2 is a declared axis the ladder already carries; the surveyor has to feed it."""

    def test_an_exempt_repo_surfaces_no_review_request(self) -> None:
        with _channel(), _reads(), _exempt("group/repo"):
            signals = MrTriageScanner(host=FakeCodeHost(user="alice", my_prs=[_opened(36)])).scan()

        assert signals == []

    def test_an_exempt_repo_still_owes_its_ci_fix(self) -> None:
        """Exemption answers for everything social and for nothing else."""
        host = FakeCodeHost(user="alice", my_prs=[_mr(37, head_pipeline={"status": "failed"})])

        with _channel(), _reads(), _exempt("group/repo"):
            signals = MrTriageScanner(host=host).scan()

        assert _actions(signals) == [TriageAction.FIX_CI]


class TestItNeverActs(TestCase):
    def test_surfacing_a_group_ping_posts_nothing_and_marks_nothing(self) -> None:
        post = _seed_request(10, days_idle=3.0)
        host = FakeCodeHost(user="alice", my_prs=[_green(10)])

        MrTriageScanner(host=host, repo_owner=lambda _slug: RepoOwner.ENGINEERING).scan()

        post.refresh_from_db()
        assert post.last_nag_at is None
        assert post.done_at is None

    def test_every_signal_carries_the_mr_and_the_reason(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_mr(11, head_pipeline={"status": "failed"})])

        signal = MrTriageScanner(host=host).scan()[0]

        assert signal.payload["url"] == f"{_REPO}/11"
        assert signal.payload["reason"]
        assert f"{_REPO}/11" in signal.summary or "11" in signal.summary
