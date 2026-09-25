"""The MR-triage surveyor surfaces verdicts and asks the owner when needed.

The scanner's whole job is to run the operator's own open MRs through the pure
ladder and say what each one needs. It never posts to colleagues or dispatches,
but it does create owner questions for missing review requests. The cases below
pin both the facts it reads and the bounded effects it owns.
"""

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.backend_factory import OverlayBackends
from teatree.core.gates.review_request_guard import GuardTarget
from teatree.core.models import ConfigSetting, DeferredQuestion, ReviewRequestPost
from teatree.core.review.mr_state_question import mr_state_marker
from teatree.core.review.mr_triage import RepoOwner, TriageAction, TriageReason
from teatree.loop.scanner_factories import _mr_triage_scanner_for
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.mr_triage_scan import MrTriageScanner
from tests.teatree_loop.test_scanners import FakeCodeHost

_REPO = "https://gitlab.example.com/group/repo/-/merge_requests"
_OTHER_REPO = "https://gitlab.example.com/other/repo/-/merge_requests"
_SCOPE = ("https://gitlab.example.com/group/",)


def _mr(iid: int, **payload: object) -> dict[str, object]:
    return {"iid": iid, "title": f"MR {iid}", "web_url": f"{_REPO}/{iid}", "sha": f"{iid:040d}", **payload}


def _green(iid: int, **payload: object) -> dict[str, object]:
    return _mr(iid, head_pipeline={"status": "success"}, **payload)


def _opened(iid: int, *, days_ago: float = 2.0, **payload: object) -> dict[str, object]:
    """A green merge request carrying the creation stamp the channel read is measured against."""
    return _green(iid, created_at=(timezone.now() - dt.timedelta(days=days_ago)).isoformat(), **payload)


def _no_pipeline(iid: int, *, days_ago: float = 2.0, **payload: object) -> dict[str, object]:
    """GitLab's list shape: an open merge request whose payload carries no pipeline at all."""
    return _mr(iid, created_at=(timezone.now() - dt.timedelta(days=days_ago)).isoformat(), **payload)


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
        overlay="",
        slack_channel_id="C1",
        slack_thread_ts=f"ts.{iid}",
        created_at=timezone.now() - dt.timedelta(days=days_idle),
    )


def _actions(signals: list[ScanSignal]) -> list[object]:
    return [s.payload["action"] for s in signals]


class _OptedIn(TestCase):
    """This file exercises the scanner itself, so each case opts the box in first."""

    def setUp(self) -> None:
        super().setUp()
        ConfigSetting.objects.set_value("mr_triage_enabled", value=True)


@dataclass
class _RecordingCiEnricher:
    urls: list[str] = field(default_factory=list)
    status: str = "failed"

    def status_for(self, *, url: str, head_sha: str) -> str:
        _ = head_sha
        self.urls.append(url)
        return self.status


class TestAListingWithNoPipelineFieldStillReadsCi(_OptedIn):
    """GitLab's MR LIST payload carries no `head_pipeline`, so the enricher IS the CI source.

    Without one every merge request on that forge reads UNKNOWN forever: a green MR waits on
    `ci_not_green` every tick, the review-request ask never fires, and a group holding an
    unreadable sibling is held for good.
    """

    def test_the_factory_wired_surveyor_reads_ci_a_gitlab_listing_never_carried(self) -> None:
        enricher = _RecordingCiEnricher(status="success")
        host = FakeCodeHost(user="alice", my_prs=[_no_pipeline(40)])
        backend = OverlayBackends(name="overlay-a", hosts=(host,), identities=("alice",))

        with patch("teatree.loop.scanner_factories._allowed_url_prefixes_for_host", return_value=_SCOPE):
            scanner = _mr_triage_scanner_for(backend, ci_enricher=enricher)
        assert scanner is not None
        with _channel(), _reads():
            signals = scanner.scan()

        assert _actions(signals) == [TriageAction.REQUEST_REVIEW]
        assert enricher.urls == [f"{_REPO}/40"]
        assert [q.dedupe_marker for q in _open_mr_questions()] == [mr_state_marker(f"{_REPO}/40")]

    def test_no_enricher_leaves_the_same_mr_waiting_on_unknown_ci(self) -> None:
        # The control: the verdict above is the enricher's doing, not the payload's.
        with _channel(), _reads():
            signals = MrTriageScanner(
                allowed_url_prefixes=_SCOPE, host=FakeCodeHost(user="alice", my_prs=[_no_pipeline(41)])
            ).scan()

        assert _actions(signals) == [TriageAction.WAIT]

    def test_a_payload_that_carries_its_own_pipeline_never_queries_the_enricher(self) -> None:
        enricher = _RecordingCiEnricher(status="success")

        with _channel(), _reads():
            MrTriageScanner(
                allowed_url_prefixes=_SCOPE,
                host=FakeCodeHost(user="alice", my_prs=[_opened(42)]),
                ci_enricher=enricher,
            ).scan()

        assert enricher.urls == [], "the payload short-circuit bounds the per-tick read budget"


class TestItSurfacesAVerdictPerMr(_OptedIn):
    def test_a_red_mr_asks_for_the_ci_fix(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_mr(1, head_pipeline={"status": "failed"})])

        signals = MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host).scan()

        assert _actions(signals) == [TriageAction.FIX_CI]

    def test_a_green_mr_with_no_ledger_row_is_an_owner_question_not_a_review_request(self) -> None:
        """No row does not prove nobody was asked, so the ladder must not act on it."""
        host = FakeCodeHost(user="alice", my_prs=[_green(2)])

        signals = MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host).scan()

        assert _actions(signals) == [TriageAction.ASK_OWNER]

    def test_a_requested_mr_past_its_window_names_the_group_ping(self) -> None:
        _seed_request(3, days_idle=3.0)
        host = FakeCodeHost(user="alice", my_prs=[_green(3)])

        signals = MrTriageScanner(
            allowed_url_prefixes=_SCOPE, host=host, repo_owner=lambda _slug: RepoOwner.ENGINEERING
        ).scan()

        assert _actions(signals) == [TriageAction.GROUP_PING]

    def test_the_same_mr_on_a_devops_repo_is_still_waiting(self) -> None:
        _seed_request(4, days_idle=3.0)
        host = FakeCodeHost(user="alice", my_prs=[_green(4)])

        signals = MrTriageScanner(
            allowed_url_prefixes=_SCOPE, host=host, repo_owner=lambda _slug: RepoOwner.DEVOPS
        ).scan()

        assert _actions(signals) == [TriageAction.WAIT]

    def test_an_approved_mr_needs_nothing(self) -> None:
        _seed_request(5, days_idle=30.0)
        host = FakeCodeHost(
            user="alice",
            my_prs=[_green(5)],
            approvals={"approvals_left": 0, "approved_by": [{"user": {"username": "bo"}}]},
        )

        signals = MrTriageScanner(host=host).scan()

        assert signals == []

    def test_a_github_shaped_approval_with_an_empty_approved_by_list_needs_nothing(self) -> None:
        """GitHub's ``get_mr_approvals`` hard-codes ``approved_by=[]`` always (#8).

        Only ``approvals_left`` carries the real verdict there. Reading ``approved_by``
        truthiness misreads every GitHub-backed approval as unapproved and keeps re-pinging it.
        """
        _seed_request(38, days_idle=30.0)
        host = FakeCodeHost(user="alice", my_prs=[_green(38)], approvals={"approvals_left": 0, "approved_by": []})

        signals = MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host).scan()

        assert signals == []

    def test_a_draft_leaves_triage_entirely(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_green(6, draft=True)])

        assert MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host).scan() == []


class TestWhatItCannotKnowBecomesAQuestion(_OptedIn):
    def test_an_mr_with_no_review_request_row_and_no_ci_is_an_owner_question(self) -> None:
        """Neither signal is readable, so the ladder's fallback is the honest answer."""
        host = FakeCodeHost(user="alice", my_prs=[_mr(7)])

        signals = MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host).scan()

        assert _actions(signals) == [TriageAction.ASK_OWNER]

    def test_an_unreadable_approval_probe_does_not_claim_the_mr_is_unapproved(self) -> None:
        _seed_request(8, days_idle=30.0)
        host = FakeCodeHost(user="alice", my_prs=[_green(8)], raise_on_approvals=RuntimeError("forge down"))

        signals = MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host).scan()

        assert _actions(signals) == [TriageAction.ASK_OWNER]


class TestItIsBoundedAndScoped(_OptedIn):
    def test_it_stops_at_the_per_tick_cap(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_green(n) for n in range(10)])

        signals = MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host, max_mrs_per_tick=3).scan()

        assert len(signals) == 3

    def test_an_mr_outside_the_overlays_url_claim_is_skipped(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_green(9)])

        signals = MrTriageScanner(host=host, allowed_url_prefixes=("https://gitlab.example.com/other/",)).scan()

        assert signals == []

    def test_the_factory_wires_the_overlay_url_claim_into_the_surveyor(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_green(9)])
        backend = OverlayBackends(name="overlay-a", hosts=(host,), identities=("alice",))

        with patch(
            "teatree.loop.scanner_factories._allowed_url_prefixes_for_host",
            return_value=("https://gitlab.example.com/other/",),
        ):
            scanner = _mr_triage_scanner_for(backend, ci_enricher=_RecordingCiEnricher())

        assert scanner is not None
        assert scanner.scan() == []


class TestAWorkGroupIsHeldUntilEveryMemberIsReady(_OptedIn):
    def test_a_red_sibling_holds_the_green_member(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                _grouped(20, head_pipeline={"status": "success"}),
                _grouped(21, head_pipeline={"status": "failed"}),
            ],
        )

        held = [
            s
            for s in MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host).scan()
            if s.payload["url"] == f"{_REPO}/20"
        ]

        assert [s.payload["action"] for s in held] == [TriageAction.WAIT]
        assert held[0].payload["reason"] is TriageReason.WORK_GROUP_NOT_READY
        assert held[0].payload["detail"] == f"{_REPO}/20"

    def test_a_draft_sibling_holds_the_group_it_belongs_to(self) -> None:
        """The draft leaves triage on its own account; its group still waits for it."""
        host = FakeCodeHost(
            user="alice",
            my_prs=[_grouped(22, head_pipeline={"status": "success"}), _grouped(23, draft=True)],
        )

        signals = MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host).scan()

        assert _actions(signals) == [TriageAction.WAIT]

    def test_a_group_whose_members_are_all_green_is_not_held(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                _grouped(24, head_pipeline={"status": "success"}),
                _grouped(25, head_pipeline={"status": "success"}),
            ],
        )

        signals = MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host).scan()

        assert _actions(signals) == [TriageAction.ASK_OWNER, TriageAction.ASK_OWNER]

    def test_a_merge_request_that_shares_no_signal_is_never_held_by_its_own_group(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_green(26), _grouped(27, head_pipeline={"status": "failed"})])

        surfaced = [
            s
            for s in MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host).scan()
            if s.payload["url"] == f"{_REPO}/26"
        ]

        assert [s.payload["action"] for s in surfaced] == [TriageAction.ASK_OWNER]
        assert surfaced[0].payload["detail"] == ""

    def test_an_outside_sibling_the_listing_cannot_show_green_holds_the_group_unread(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                _grouped(28, head_pipeline={"status": "success"}, created_at=timezone.now().isoformat()),
                _grouped(29, repo=_OTHER_REPO),
            ],
        )
        enricher = _RecordingCiEnricher()
        scanner = MrTriageScanner(host=host, allowed_url_prefixes=(f"{_REPO}/",), ci_enricher=enricher)

        with _channel(), _reads():
            survey = scanner._survey(("alice",))
            signals = scanner.scan()

        assert list(survey.merge_requests) == [f"{_REPO}/28"]
        assert [(s.payload["url"], s.payload["action"]) for s in signals] == [(f"{_REPO}/28", TriageAction.WAIT)]
        assert signals[0].payload["reason"] is TriageReason.WORK_GROUP_NOT_READY
        assert enricher.urls == []
        assert _open_mr_questions() == []

    def test_an_outside_red_sibling_holds_the_green_member(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[
                _grouped(30, head_pipeline={"status": "success"}),
                _grouped(31, repo=_OTHER_REPO, head_pipeline={"status": "failed"}),
            ],
        )

        signals = MrTriageScanner(host=host, allowed_url_prefixes=(f"{_REPO}/",)).scan()

        assert [(s.payload["url"], s.payload["action"]) for s in signals] == [(f"{_REPO}/30", TriageAction.WAIT)]


class TestAnUnresolvedScopeReadsNothing(_OptedIn):
    """An empty scope is an unresolved one — never every merge request the credential can list."""

    def test_an_empty_scope_neither_lists_nor_asks_the_owner(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_opened(34)])

        with _channel(), _reads(), patch.object(host, "list_my_prs", wraps=host.list_my_prs) as listing:
            signals = MrTriageScanner(host=host, allowed_url_prefixes=()).scan()

        assert signals == []
        listing.assert_not_called()
        assert _open_mr_questions() == []

    def test_the_factory_with_an_unresolved_scope_asks_the_owner_nothing(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_opened(35)])
        backend = OverlayBackends(name="overlay-a", hosts=(host,), identities=("alice",))

        with patch("teatree.loop.scanner_factories._allowed_url_prefixes_for_host", return_value=()):
            scanner = _mr_triage_scanner_for(backend, ci_enricher=_RecordingCiEnricher())
        assert scanner is not None
        with _channel(), _reads():
            assert scanner.scan() == []

        assert _open_mr_questions() == []


class TestAMissingReviewIsProvedAgainstTheChannel(_OptedIn):
    """The one fact no ledger can supply: nobody has been asked yet.

    An absent ledger row is silence, so the surveyor reads the review channel
    itself. What it earns there is the ladder's review-request rung — the first
    time a merge request nobody ever asked about becomes visible as such.
    """

    def test_a_green_mr_the_channel_never_carried_is_a_review_request(self) -> None:
        with _channel(), _reads():
            signals = MrTriageScanner(
                allowed_url_prefixes=_SCOPE, host=FakeCodeHost(user="alice", my_prs=[_opened(30)])
            ).scan()

        assert _actions(signals) == [TriageAction.REQUEST_REVIEW]

    def test_an_ask_already_in_the_channel_is_not_a_second_request(self) -> None:
        """Asked out of band, so the ledger holds no clock — it waits rather than ask again."""
        with _channel(), _reads(asked=(f"{_REPO}/31",)):
            signals = MrTriageScanner(
                allowed_url_prefixes=_SCOPE, host=FakeCodeHost(user="alice", my_prs=[_opened(31)])
            ).scan()

        assert _actions(signals) == [TriageAction.WAIT]
        assert _open_mr_questions() == []

    def test_a_failed_channel_read_is_never_a_missing_review(self) -> None:
        with _channel(), _reads(ok=False):
            signals = MrTriageScanner(
                allowed_url_prefixes=_SCOPE, host=FakeCodeHost(user="alice", my_prs=[_opened(32)])
            ).scan()

        assert _actions(signals) == [TriageAction.ASK_OWNER]

    def test_the_missing_review_reaches_the_owner_not_only_the_statusline(self) -> None:
        with _channel(), _reads():
            MrTriageScanner(allowed_url_prefixes=_SCOPE, host=FakeCodeHost(user="alice", my_prs=[_opened(33)])).scan()

        assert [q.dedupe_marker for q in _open_mr_questions()] == [mr_state_marker(f"{_REPO}/33")]

    def test_a_held_group_member_asks_the_owner_nothing(self) -> None:
        host = FakeCodeHost(
            user="alice",
            my_prs=[_opened(34, title="feat: part a (repo#7)"), _opened(35, title="feat: part b (repo#7)", draft=True)],
        )

        with _channel(), _reads():
            MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host).scan()

        assert _open_mr_questions() == []


class TestAReviewExemptRepoIsNeverAskedAbout(_OptedIn):
    """R2 is a declared axis the ladder already carries; the surveyor has to feed it."""

    def test_an_exempt_repo_surfaces_no_review_request(self) -> None:
        with _channel(), _reads(), _exempt("group/repo"):
            signals = MrTriageScanner(
                allowed_url_prefixes=_SCOPE, host=FakeCodeHost(user="alice", my_prs=[_opened(36)])
            ).scan()

        assert signals == []

    def test_an_exempt_repo_still_owes_its_ci_fix(self) -> None:
        """Exemption answers for everything social and for nothing else."""
        host = FakeCodeHost(user="alice", my_prs=[_mr(37, head_pipeline={"status": "failed"})])

        with _channel(), _reads(), _exempt("group/repo"):
            signals = MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host).scan()

        assert _actions(signals) == [TriageAction.FIX_CI]


class TestItNeverPostsToColleagues(_OptedIn):
    def test_surfacing_a_group_ping_posts_nothing_and_marks_nothing(self) -> None:
        post = _seed_request(10, days_idle=3.0)
        host = FakeCodeHost(user="alice", my_prs=[_green(10)])

        MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host, repo_owner=lambda _slug: RepoOwner.ENGINEERING).scan()

        post.refresh_from_db()
        assert post.last_nag_at is None
        assert post.done_at is None

    def test_every_signal_carries_the_mr_and_the_reason(self) -> None:
        host = FakeCodeHost(user="alice", my_prs=[_mr(11, head_pipeline={"status": "failed"})])

        signal = MrTriageScanner(allowed_url_prefixes=_SCOPE, host=host).scan()[0]

        assert signal.payload["url"] == f"{_REPO}/11"
        assert signal.payload["reason"]
        assert f"{_REPO}/11" in signal.summary or "11" in signal.summary


class TestTheSurveyorShipsOff(TestCase):
    """A box builds no surveyor until ``mr_triage_enabled`` opts it in."""

    def test_the_factory_builds_nothing_by_default(self) -> None:
        backend = OverlayBackends(name="overlay-a", hosts=(FakeCodeHost(user="alice"),), identities=("alice",))

        assert _mr_triage_scanner_for(backend, ci_enricher=_RecordingCiEnricher(status="success")) is None
