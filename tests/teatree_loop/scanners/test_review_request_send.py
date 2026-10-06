"""The followup flow sends the review request the triage ladder finds owed (#4941).

Driven through ``jobs_for_domain(Domain.FOLLOWUP, …)`` with the sanctioned
``review_request_post`` command running for real. Only the forge, the Slack
transport, the review channel's history and the review-channel configuration are
replaced, so every gate between the ladder and the post is the live one.
"""

import datetime as dt
import io
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import DraftState
from teatree.core.gates.review_request_guard import GuardTarget
from teatree.core.models import ConfigSetting, DeferredQuestion, PullRequest, ReviewRequestPost, ReviewVerdict, Ticket
from teatree.core.review.mr_state_question import ask_mr_state, mr_state_marker
from teatree.loop import followup_dry_run
from teatree.loop.domain_jobs import jobs_for_domain
from teatree.loop.job_identity import Domain
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.mr_triage_scan import MrTriageScanner
from teatree.loop.scanners.review_request_send import ReviewRequestSendScanner
from teatree.types import RawAPIDict
from tests.teatree_core._on_behalf_gate_helpers import seed_forbidding_posture, seed_permitting_posture
from tests.teatree_loop.test_followup_dry_run import _file_backed_default_database, _InlineThreadPoolExecutor
from tests.teatree_loop.test_scanners import FakeCodeHost

_OVERLAY = "t3-teatree"
_SLUG = "group/repo"
_URL = f"https://gitlab.example.com/{_SLUG}/-/merge_requests/7"
_SCOPE = ("https://gitlab.example.com/group/",)
_HEAD = "7" * 40
_OLD_HEAD = "6" * 40
_CHANNEL = "C_REVIEW"
_SENDER = "review_request_send"


def _mr(iid: int = 7, *, sha: str = _HEAD) -> RawAPIDict:
    return {
        "iid": iid,
        "title": f"fix: thing {iid}",
        "web_url": f"https://gitlab.example.com/{_SLUG}/-/merge_requests/{iid}",
        "sha": sha,
        "head_pipeline": {"status": "success"},
        "created_at": (timezone.now() - dt.timedelta(days=1)).isoformat(),
    }


@dataclass
class _Forge(FakeCodeHost):
    def fetch_pr_draft_state(self, *, slug: str, pr_id: int) -> DraftState:
        _ = (slug, pr_id)
        return DraftState.NOT_DRAFT

    def get_pr_author(self, *, pr_url: str) -> str:
        _ = pr_url
        return self.user


@dataclass(frozen=True)
class _Match:
    pr_url: str
    ts: str
    permalink: str = ""
    author: str = ""


@dataclass(frozen=True)
class _Read:
    ok: bool
    matches: list[_Match]


@dataclass
class _Slack:
    """The review channel: what was posted to it is what a later history read finds."""

    posts: list[tuple[str, str, str]] = field(default_factory=list)

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = thread_ts
        ts = f"{time.time():.6f}"
        self.posts.append((channel, text, ts))
        return {"ok": True, "ts": ts}

    def get_permalink(self, *, channel: str, ts: str) -> str:
        return f"https://slack.example/archives/{channel}/p{ts.replace('.', '')}"

    def read_recent_review_matches(self, spec: object) -> _Read:
        urls = getattr(spec, "pr_urls", [])
        return _Read(
            ok=True,
            matches=[_Match(pr_url=url, ts=ts) for url in urls for _channel, text, ts in self.posts if url in text],
        )


def _ready_ticket(
    *, iid: int = 7, verdict_at: str = _HEAD, verdict: str = "merge_safe", attested: bool = True
) -> Ticket:
    ticket = Ticket.objects.create(overlay=_OVERLAY, state=Ticket.State.PR_OPENED)
    PullRequest.objects.create(ticket=ticket, url=_mr(iid)["web_url"], repo=_SLUG, iid=str(iid))
    if attested:
        ticket.record_anti_vacuity_attestation(_HEAD, "ACs checked against diff", [], no_new_tests=True)
    ReviewVerdict.record(
        pr_id=iid,
        slug=_SLUG,
        reviewed_sha=verdict_at,
        verdict=verdict,
        reviewer_identity="cold-reviewer",
        ticket=ticket,
    )
    return ticket


@contextmanager
def _world(slack: _Slack, forge: _Forge) -> Iterator[Path]:
    """The four replaced edges; everything between them is the live code."""
    target = GuardTarget(channel_id=_CHANNEL, channel_name="reviews", token="xoxb-bot")
    provider = type("P", (), {"read_recent_review_matches": staticmethod(slack.read_recent_review_matches)})
    command = "teatree.core.management.commands.review_request_post"
    with tempfile.TemporaryDirectory() as data_dir, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"T3_DATA_DIR": data_dir}))
        stack.enter_context(patch("teatree.loop.scanner_factories._allowed_url_prefixes_for_host", return_value=_SCOPE))
        stack.enter_context(patch("teatree.loop.scanners.mr_triage_scan.resolve_guard_target", return_value=target))
        stack.enter_context(patch(f"{command}.resolve_guard_target", return_value=target))
        stack.enter_context(patch(f"{command}.messaging_from_overlay", return_value=slack))
        stack.enter_context(patch(f"{command}.code_host_from_overlay", return_value=forge))
        stack.enter_context(patch("teatree.core.backend_factory.code_host_from_overlay", return_value=forge))
        stack.enter_context(
            patch("teatree.core.gates.review_request_batch_gate.code_host_from_overlay", return_value=forge)
        )
        stack.enter_context(patch("teatree.core.backend_registry.get_backend_provider", return_value=provider))
        yield Path(data_dir)


def _backend(forge: _Forge, slack: _Slack) -> OverlayBackends:
    return OverlayBackends(name=_OVERLAY, hosts=(forge,), messaging=slack, identities=("alice",))


def _followup_pass(forge: _Forge, slack: _Slack) -> list[ScanSignal]:
    """One FOLLOWUP pass, running the review-request sender the domain selected."""
    jobs = jobs_for_domain(Domain.FOLLOWUP, _backend(forge, slack))
    senders = [job.scanner for job in jobs if job.scanner.name == _SENDER]
    assert len(senders) == 1, [job.scanner.name for job in jobs]
    return senders[0].scan()


def _kinds(signals: list[ScanSignal]) -> list[str]:
    return [signal.kind for signal in signals]


def _mr_state_questions() -> list[str]:
    return list(
        DeferredQuestion.pending().filter(dedupe_marker__startswith="mr-state:").values_list("dedupe_marker", flat=True)
    )


class _SenderCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        seed_permitting_posture()
        ConfigSetting.objects.set_value("user_identity_aliases", ["alice"])
        self.forge = _Forge(user="alice", my_prs=[_mr()])
        self.slack = _Slack()


class TestAnEligibleMrGetsExactlyOneRequest(_SenderCase):
    def test_one_pass_posts_one_request_and_records_it(self) -> None:
        _ready_ticket()

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert [(channel, _URL in text) for channel, text, _ts in self.slack.posts] == [(_CHANNEL, True)]
        rows = list(ReviewRequestPost.objects.filter(mr_url=_URL))
        assert [(row.slack_thread_ts, row.overlay) for row in rows] == [(self.slack.posts[0][2], _OVERLAY)]
        assert _kinds(signals) == ["review_request.sent"]

    def test_a_second_pass_recognises_the_request_and_posts_nothing(self) -> None:
        _ready_ticket()
        with _world(self.slack, self.forge):
            _followup_pass(self.forge, self.slack)
            before = list(ReviewRequestPost.objects.filter(mr_url=_URL).values())

            signals = _followup_pass(self.forge, self.slack)

        assert len(self.slack.posts) == 1
        assert list(ReviewRequestPost.objects.filter(mr_url=_URL).values()) == before
        assert "review_request.sent" not in _kinds(signals)


class TestOnlyACurrentColdReviewReleasesTheRequest(_SenderCase):
    def test_a_verdict_at_an_older_head_sends_nothing(self) -> None:
        _ready_ticket(verdict_at=_OLD_HEAD)

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert not ReviewRequestPost.objects.filter(mr_url=_URL).exists()
        assert _kinds(signals) == ["review_request.send_deferred"]
        assert len(_mr_state_questions()) == 1

    def test_a_hold_at_the_head_sends_nothing(self) -> None:
        _ready_ticket(verdict="hold")

        with _world(self.slack, self.forge):
            _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert not ReviewRequestPost.objects.filter(mr_url=_URL).exists()


class TestARefusalTheOwnerCanFixReachesTheOwner(_SenderCase):
    def test_no_resolvable_ticket_asks_once_and_posts_nothing(self) -> None:
        ReviewVerdict.record(pr_id=7, slug=_SLUG, reviewed_sha=_HEAD, verdict="merge_safe", reviewer_identity="cold")

        with _world(self.slack, self.forge):
            first = _followup_pass(self.forge, self.slack)
            _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert _mr_state_questions() == [mr_state_marker(_URL)]
        assert _kinds(first) == ["review_request.send_refused"]
        assert first[0].payload["reason"] == "no_ticket"

    def test_a_missing_anti_vacuity_attestation_is_a_refusal_the_owner_sees(self) -> None:
        _ready_ticket(attested=False)

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert [signal.payload["reason"] for signal in signals] == ["anti_vacuity_not_attested"]
        assert _mr_state_questions() == [mr_state_marker(_URL)]
        assert not ReviewRequestPost.objects.filter(mr_url=_URL).exists()

    def test_a_send_retires_the_owner_question_it_answers(self) -> None:
        ask_mr_state(mr_url=_URL, reason="it was not sendable before.")
        _ready_ticket()

        with _world(self.slack, self.forge):
            _followup_pass(self.forge, self.slack)

        assert len(self.slack.posts) == 1
        assert _mr_state_questions() == []
        assert DeferredQuestion.objects.get(dedupe_marker=mr_state_marker(_URL)).dismissed_at is not None


class TestTheOwnerQuestionLivesWhereTheSenderIsAbsent(_SenderCase):
    """The triage surveyor still asks when no sender can act on its verdict."""

    def _ship_triage_scan(self) -> list[ScanSignal]:
        backend = _backend(self.forge, self.slack)
        jobs = jobs_for_domain(Domain.SHIP, backend, all_backends=(backend,))
        (triage,) = [job.scanner for job in jobs if job.scanner.name == "mr_triage"]
        return triage.scan()

    def test_a_permitting_posture_leaves_the_ask_to_the_sender(self) -> None:
        with _world(self.slack, self.forge):
            self._ship_triage_scan()

        assert _mr_state_questions() == []

    def test_a_forbidding_posture_still_asks_the_owner(self) -> None:
        seed_forbidding_posture()

        with _world(self.slack, self.forge):
            self._ship_triage_scan()

        assert _mr_state_questions() == [mr_state_marker(_URL)]


class TestTheSenderFollowsThePosture(_SenderCase):
    def test_a_forbidding_posture_selects_no_sender(self) -> None:
        seed_forbidding_posture()

        names = {job.scanner.name for job in jobs_for_domain(Domain.FOLLOWUP, _backend(self.forge, self.slack))}

        assert _SENDER not in names


class TestABurstIsBoundedButNeverStranded(_SenderCase):
    def test_three_go_out_a_pass_and_the_fourth_goes_out_next(self) -> None:
        self.forge.my_prs = [_mr(iid) for iid in (1, 2, 3, 4)]
        for iid in (1, 2, 3, 4):
            _ready_ticket(iid=iid)

        with _world(self.slack, self.forge):
            _followup_pass(self.forge, self.slack)
            first = len(self.slack.posts)
            _followup_pass(self.forge, self.slack)
            second = len(self.slack.posts)
            _followup_pass(self.forge, self.slack)

        assert (first, second, len(self.slack.posts)) == (3, 4, 4)
        assert ReviewRequestPost.objects.count() == 4

    def test_mrs_awaiting_review_do_not_use_up_the_pass(self) -> None:
        self.forge.my_prs = [_mr(iid) for iid in (1, 2, 3, 4)]
        for iid in (1, 2, 3):
            _ready_ticket(iid=iid, verdict_at=_OLD_HEAD)
        _ready_ticket(iid=4)

        with _world(self.slack, self.forge):
            _followup_pass(self.forge, self.slack)

        assert [_mr(4)["web_url"] in text for _channel, text, _ts in self.slack.posts] == [True]


class TestAnUnreadableAnswerIsRetriedNotTrusted(_SenderCase):
    def _scanner(self) -> ReviewRequestSendScanner:
        return ReviewRequestSendScanner(
            triage=MrTriageScanner(host=self.forge, overlay_name=_OVERLAY, allowed_url_prefixes=_SCOPE)
        )

    def test_a_command_that_prints_no_verdict_is_deferred(self) -> None:
        _ready_ticket()

        def _prose_only(*_args: object, stream: io.StringIO) -> None:
            stream.write("Usage: review_request_post [OPTIONS]\n")
            raise SystemExit(2)

        with _world(self.slack, self.forge), patch("teatree.core.machine_output.call_command_streamed", _prose_only):
            signals = self._scanner().scan()

        assert [(signal.kind, signal.payload["reason"]) for signal in signals] == [
            ("review_request.send_deferred", "refused:unreadable_result")
        ]
        assert _mr_state_questions() == []

    def test_a_payload_with_no_head_sha_is_deferred(self) -> None:
        _ready_ticket()
        self.forge.my_prs = [_mr(sha="")]

        with _world(self.slack, self.forge):
            signals = self._scanner().scan()

        assert [(signal.kind, signal.payload["reason"]) for signal in signals] == [
            ("review_request.send_deferred", "head_unreadable")
        ]
        assert self.slack.posts == []


class TestTheFollowupPreviewSendsNothing(TransactionTestCase):
    def test_the_preview_reports_the_owed_request_and_makes_none_of_it(self) -> None:
        seed_permitting_posture()
        ConfigSetting.objects.set_value("user_identity_aliases", ["alice"])
        forge = _Forge(user="alice", my_prs=[_mr()])
        slack = _Slack()
        _ready_ticket()

        with (
            _file_backed_default_database(),
            patch.object(followup_dry_run, "_SubmitterContextPool", _InlineThreadPoolExecutor),
            _world(slack, forge) as data_dir,
        ):
            report = followup_dry_run.run_followup_dry_run(
                lambda: jobs_for_domain(Domain.FOLLOWUP, _backend(forge, slack))
            )
            persisted = list(data_dir.rglob("mr_review_messages.json"))

        actions = [(entry.suppressed_action, entry.outcome) for entry in report.candidates if entry.event == "action"]
        assert ("review_request_post", "posted") in actions, report.candidates
        assert _SENDER in report.selected_scanners
        assert report.breach_count == 0
        assert report.errors == {}
        assert slack.posts == []
        assert persisted == []
        assert not ReviewRequestPost.objects.exists()
