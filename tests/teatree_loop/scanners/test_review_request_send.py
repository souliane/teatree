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
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import httpx
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import DraftState
from teatree.core.gates.review_request_guard import GuardTarget
from teatree.core.modelkit.forge_readability import HEAD_SHA_UNREADABLE
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.models import (
    ConfigSetting,
    DeferredQuestion,
    Loop,
    PullRequest,
    ReviewRequestPost,
    ReviewVerdict,
    Ticket,
)
from teatree.core.review.mr_state_question import OwnerAsk, ask_mr_state, head_tag, mr_state_marker
from teatree.loop import followup_dry_run
from teatree.loop.domain_jobs import jobs_for_domain
from teatree.loop.job_identity import Domain
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.mr_triage_scan import MISSING_REVIEW_OPTIONS, MrTriageScanner
from teatree.loop.scanners.review_request_send import (
    CallCommandReviewRequestPoster,
    ReviewRequestPoster,
    ReviewRequestSendScanner,
)
from teatree.types import RawAPIDict
from tests._owner_channel import answer_on_slack
from tests._send_gate import allow_slack_channels
from tests.teatree_core._on_behalf_gate_helpers import seed_forbidding_posture, seed_permitting_posture
from tests.teatree_loop.test_followup_dry_run import _file_backed_default_database, _InlineThreadPoolExecutor
from tests.teatree_loop.test_scanners import FakeCodeHost

_OVERLAY = "t3-teatree"
_SLUG = "group/repo"
_URL = f"https://gitlab.example.com/{_SLUG}/-/merge_requests/7"
_SCOPE = ("https://gitlab.example.com/group/",)
_HEAD = "7" * 40
_OLD_HEAD = "6" * 40
_NEW_HEAD = "8" * 40
_CHANNEL = "C_REVIEW"
_SENDER = "review_request_send"
_COMMAND = "teatree.core.management.commands.review_request_post"
_POST, _IN_PERSON, _NOT_READY = MISSING_REVIEW_OPTIONS
_ASKED_AT_HEAD = mr_state_marker(_URL, head_sha=_HEAD)


def _mr(iid: int = 7, *, sha: str = _HEAD, title: str = "") -> RawAPIDict:
    title = title or f"fix: thing {iid}"
    return {
        "iid": iid,
        "title": title,
        "description": f"{title}\n\n## What\nThe thing.\n\n## Why\nIt was broken.\n",
        "web_url": f"https://gitlab.example.com/{_SLUG}/-/merge_requests/{iid}",
        "sha": sha,
        "head_pipeline": {"status": "success"},
        "created_at": (timezone.now() - dt.timedelta(days=1)).isoformat(),
    }


@dataclass
class _Forge(FakeCodeHost):
    author: str = ""
    author_error: Exception | None = None
    draft: DraftState = DraftState.NOT_DRAFT
    live_head: str | None = None

    def fetch_pr_draft_state(self, *, slug: str, pr_id: int) -> DraftState:
        _ = (slug, pr_id)
        return self.draft

    def get_pr_author(self, *, pr_url: str) -> str:
        _ = pr_url
        if self.author_error is not None:
            raise self.author_error
        return self.author or self.user

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        _ = slug
        if self.live_head is not None:
            return self.live_head
        return next((str(pr["sha"]) for pr in self.my_prs if pr["iid"] == pr_id), "")


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
    fail_next: Exception | None = None
    refusal: RawAPIDict | None = None

    def post_message(self, *, channel: str, text: str, thread_ts: str = "") -> RawAPIDict:
        _ = thread_ts
        if self.fail_next is not None:
            failure, self.fail_next = self.fail_next, None
            raise failure
        if self.refusal is not None:
            return self.refusal
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
    with tempfile.TemporaryDirectory() as data_dir, ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"T3_DATA_DIR": data_dir}))
        stack.enter_context(patch("teatree.loop.scanner_factories._allowed_url_prefixes_for_host", return_value=_SCOPE))
        stack.enter_context(patch("teatree.loop.scanners.mr_triage_scan.resolve_guard_target", return_value=target))
        stack.enter_context(patch(f"{_COMMAND}.resolve_guard_target", return_value=target))
        stack.enter_context(patch(f"{_COMMAND}.messaging_from_overlay", return_value=slack))
        stack.enter_context(patch(f"{_COMMAND}.code_host_from_overlay", return_value=forge))
        stack.enter_context(patch("teatree.core.backend_factory.code_host_from_overlay", return_value=forge))
        stack.enter_context(
            patch("teatree.core.gates.review_request_batch_gate.code_host_from_overlay", return_value=forge)
        )
        stack.enter_context(patch("teatree.core.backend_registry.get_backend_provider", return_value=provider))
        yield Path(data_dir)


def _backend(forge: _Forge, slack: _Slack | None) -> OverlayBackends:
    return OverlayBackends(name=_OVERLAY, hosts=(forge,), messaging=slack, identities=("alice",))


def _followup_pass(forge: _Forge, slack: _Slack) -> list[ScanSignal]:
    """One FOLLOWUP pass, running the review-request sender the domain selected."""
    jobs = jobs_for_domain(Domain.FOLLOWUP, _backend(forge, slack))
    senders = [job.scanner for job in jobs if job.scanner.name == _SENDER]
    assert len(senders) == 1, [job.scanner.name for job in jobs]
    return senders[0].scan()


def _ship_triage_pass(forge: _Forge, slack: _Slack | None) -> list[ScanSignal]:
    """One SHIP pass of the triage surveyor the domain selected."""
    backend = _backend(forge, slack)
    jobs = jobs_for_domain(Domain.SHIP, backend, all_backends=(backend,))
    (triage,) = [job.scanner for job in jobs if job.scanner.name == "mr_triage"]
    return triage.scan()


def _kinds(signals: list[ScanSignal]) -> list[str]:
    return [signal.kind for signal in signals]


def _verdicts(signals: list[ScanSignal]) -> list[tuple[str, str]]:
    return [(signal.kind, signal.payload["reason"]) for signal in signals]


def _mr_state_questions() -> list[str]:
    return list(
        DeferredQuestion.pending().filter(dedupe_marker__startswith="mr-state:").values_list("dedupe_marker", flat=True)
    )


def _pending_question(url: str = _URL) -> DeferredQuestion:
    return DeferredQuestion.pending().get(dedupe_marker__startswith=mr_state_marker(url))


def _answer(text: str) -> None:
    answer_on_slack(_pending_question(), text)


def _admit_followup(*, runs: bool) -> None:
    Loop.objects.set_manual_override(Domain.FOLLOWUP.value, runs=runs, reason="the review-request sender under test")


class _SenderCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        seed_permitting_posture()
        allow_slack_channels(_CHANNEL)
        ConfigSetting.objects.set_value("user_identity_aliases", ["alice"])
        self.forge = _Forge(user="alice", my_prs=[_mr()])
        self.slack = _Slack()

    def _scanner(self, poster: ReviewRequestPoster | None = None) -> ReviewRequestSendScanner:
        triage = MrTriageScanner(host=self.forge, overlay_name=_OVERLAY, allowed_url_prefixes=_SCOPE)
        if poster is None:
            return ReviewRequestSendScanner(triage=triage)
        return ReviewRequestSendScanner(triage=triage, poster=poster)


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
    def test_a_verdict_at_an_older_head_asks_the_owner_and_sends_nothing(self) -> None:
        _ready_ticket(verdict_at=_OLD_HEAD)

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert not ReviewRequestPost.objects.filter(mr_url=_URL).exists()
        assert _verdicts(signals) == [("review_request.send_refused", "awaiting_cold_review")]
        assert signals[0].payload["asked"] is True
        assert head_tag(_HEAD) in _pending_question().question

    def test_post_ask_is_public_post_owner_row_with_checked(self) -> None:
        _ready_ticket(verdict_at=_OLD_HEAD)

        with _world(self.slack, self.forge):
            _followup_pass(self.forge, self.slack)

        question = _pending_question()
        assert question.audience == DeferredQuestion.Audience.OWNER_QUESTION
        assert question.evidence["decision"] == "public_post"
        checked = " | ".join(question.evidence["checked"])
        assert all(fact in checked for fact in ("awaiting_cold_review", _HEAD[:12], "request_review"))

    def test_a_hold_at_the_head_sends_nothing(self) -> None:
        _ready_ticket(verdict="hold")

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert not ReviewRequestPost.objects.filter(mr_url=_URL).exists()
        assert _verdicts(signals) == [("review_request.send_deferred", "hold_at_head")]

    def test_a_hold_landing_retires_the_question_about_the_missing_review(self) -> None:
        ticket = _ready_ticket(verdict_at=_OLD_HEAD)

        with _world(self.slack, self.forge):
            _followup_pass(self.forge, self.slack)
            asked_before_the_hold = _mr_state_questions()
            ReviewVerdict.record(
                pr_id=7, slug=_SLUG, reviewed_sha=_HEAD, verdict="hold", reviewer_identity="cold", ticket=ticket
            )
            signals = _followup_pass(self.forge, self.slack)

        assert asked_before_the_hold == [_ASKED_AT_HEAD]
        assert _verdicts(signals) == [("review_request.send_deferred", "hold_at_head")]
        assert _mr_state_questions() == []

    def test_a_hold_leaves_another_callers_question_open(self) -> None:
        ask_mr_state(mr_url=_URL, reason="its work group is not ready.")
        _ready_ticket(verdict="hold")

        with _world(self.slack, self.forge):
            _followup_pass(self.forge, self.slack)

        assert _mr_state_questions() == [mr_state_marker(_URL)]

    def test_a_hold_beside_a_later_pass_from_another_reviewer_is_not_sent(self) -> None:
        ticket = _ready_ticket(verdict="hold")
        ReviewVerdict.objects.filter(ticket=ticket).update(recorded_at=timezone.now() - dt.timedelta(hours=1))
        ReviewVerdict.record(
            pr_id=7,
            slug=_SLUG,
            reviewed_sha=_HEAD,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer-b",
            ticket=ticket,
        )

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert _verdicts(signals) == [("review_request.send_deferred", "contested_hold_at_head")]
        assert "cold-reviewer-b" in signals[0].payload["detail"]
        assert _mr_state_questions() == []


class TestARefusalTheOwnerCanFixReachesTheOwner(_SenderCase):
    def test_no_resolvable_ticket_asks_once_and_posts_nothing(self) -> None:
        ReviewVerdict.record(pr_id=7, slug=_SLUG, reviewed_sha=_HEAD, verdict="merge_safe", reviewer_identity="cold")

        with _world(self.slack, self.forge):
            first = _followup_pass(self.forge, self.slack)
            _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert _mr_state_questions() == [_ASKED_AT_HEAD]
        assert _kinds(first) == ["review_request.send_refused"]
        assert first[0].payload["reason"] == "no_ticket"

    def test_a_missing_anti_vacuity_attestation_is_a_refusal_the_owner_sees(self) -> None:
        _ready_ticket(attested=False)

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert [signal.payload["reason"] for signal in signals] == ["anti_vacuity_not_attested"]
        assert _mr_state_questions() == [_ASKED_AT_HEAD]
        assert not ReviewRequestPost.objects.filter(mr_url=_URL).exists()

    def test_a_send_retires_the_owner_question_it_answers(self) -> None:
        ask_mr_state(mr_url=_URL, reason="it was not sendable before.")
        _ready_ticket()

        with _world(self.slack, self.forge):
            _followup_pass(self.forge, self.slack)

        assert len(self.slack.posts) == 1
        assert _mr_state_questions() == []
        assert DeferredQuestion.objects.get(dedupe_marker=mr_state_marker(_URL)).dismissed_at is not None

    def test_an_invalid_title_is_refused_to_the_owner_and_not_sent(self) -> None:
        self.forge.my_prs = [_mr(title="thing without a type")]
        _ready_ticket()

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert _verdicts(signals) == [("review_request.send_refused", "pr_metadata_invalid")]
        assert _mr_state_questions() == [_ASKED_AT_HEAD]

    def test_a_refusal_the_cap_keeps_from_the_owner_says_it_did_not_ask(self) -> None:
        for other in (1, 2):
            ask_mr_state(
                mr_url=_mr(other)["web_url"],
                reason="another merge request waits on the owner.",
                owner=OwnerAsk(OwnerDecision.PUBLIC_POST, ["review request refused: no_ticket"]),
            )
        _ready_ticket(attested=False)

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert _verdicts(signals) == [("review_request.send_refused", "anti_vacuity_not_attested")]
        assert signals[0].payload["asked"] is False
        assert "not asked" in signals[0].summary


class TestAnOutcomeTheSenderCannotRetryReachesTheOwner(_SenderCase):
    """A permanent refusal asks the owner; only the known transient outcomes retry quietly."""

    def _one_pass(self, extra: AbstractContextManager[object] | None = None) -> list[ScanSignal]:
        _ready_ticket()
        with _world(self.slack, self.forge), extra or nullcontext():
            return _followup_pass(self.forge, self.slack)

    def _assert_asked(self, signals: list[ScanSignal], reason: str) -> None:
        assert self.slack.posts == []
        assert _verdicts(signals) == [("review_request.send_refused", reason)]
        assert signals[0].payload["asked"] is True
        assert head_tag(_HEAD) in _pending_question().question

    def test_a_post_the_transport_did_not_land_asks_the_owner(self) -> None:
        self.slack.refusal = {"ok": False, "error": "channel_not_found"}

        self._assert_asked(self._one_pass(), "post_failed")

    def test_a_colleagues_merge_request_asks_the_owner(self) -> None:
        self.forge.author = "mallory"

        self._assert_asked(self._one_pass(), "foreign_author")

    def test_no_messaging_backend_asks_the_owner(self) -> None:
        signals = self._one_pass(patch(f"{_COMMAND}.messaging_from_overlay", return_value=None))

        self._assert_asked(signals, "no_messaging_backend")

    def test_a_claim_another_pass_holds_is_retried_quietly(self) -> None:
        ReviewRequestPost.objects.create(mr_url=_URL, overlay=_OVERLAY, slack_channel_id=_CHANNEL, slack_thread_ts="")

        signals = self._one_pass()

        assert _verdicts(signals) == [("review_request.send_deferred", "suppress:already_claimed")]
        assert _mr_state_questions() == []

    def test_an_unreadable_channel_is_retried_quietly(self) -> None:
        self._assert_a_guard_suppression_is_quiet("read_failed_failsafe")

    def test_a_request_the_guard_finds_already_posted_is_retried_quietly(self) -> None:
        self._assert_a_guard_suppression_is_quiet("already_posted")

    def _assert_a_guard_suppression_is_quiet(self, reason: str) -> None:
        _ready_ticket()
        poster = _AnsweringPoster({"action": "suppress", "reason": reason})

        with _world(self.slack, self.forge):
            signals = self._scanner(poster).scan()

        assert _verdicts(signals) == [("review_request.send_deferred", f"suppress:{reason}")]
        assert _mr_state_questions() == []

    def test_an_unreadable_author_is_retried_quietly(self) -> None:
        self.forge.author_error = RuntimeError("forge unreachable")

        signals = self._one_pass()

        assert _verdicts(signals) == [("review_request.send_deferred", "refused:authorship_unreadable")]
        assert _mr_state_questions() == []

    def test_an_unreadable_draft_state_is_retried_quietly(self) -> None:
        self.forge.draft = DraftState.UNKNOWN

        signals = self._one_pass()

        assert _verdicts(signals) == [("review_request.send_deferred", "refused:draft_state_unknown")]
        assert _mr_state_questions() == []


@dataclass
class _AnsweringPoster:
    verdict: RawAPIDict

    def post(self, *, mr_url: str, title: str, ticket_id: str, head_sha: str) -> RawAPIDict:
        _ = (mr_url, title, ticket_id, head_sha)
        return self.verdict


@dataclass
class _FailingFirstPoster:
    failing_url: str
    real: CallCommandReviewRequestPoster = field(default_factory=CallCommandReviewRequestPoster)

    def post(self, *, mr_url: str, title: str, ticket_id: str, head_sha: str) -> RawAPIDict:
        if mr_url == self.failing_url:
            msg = "the poster broke"
            raise RuntimeError(msg)
        return self.real.post(mr_url=mr_url, title=title, ticket_id=ticket_id, head_sha=head_sha)


class TestAFailedSendNeverStrandsTheMr(_SenderCase):
    def test_a_transport_error_is_deferred_and_the_next_pass_sends(self) -> None:
        _ready_ticket()
        self.slack.fail_next = httpx.ConnectError("connection refused")

        with _world(self.slack, self.forge):
            first = _followup_pass(self.forge, self.slack)
            rows_after_failure = ReviewRequestPost.objects.count()
            second = _followup_pass(self.forge, self.slack)

        assert _verdicts(first) == [("review_request.send_deferred", "error:ConnectError")]
        assert rows_after_failure == 0
        assert _kinds(second) == ["review_request.sent"]
        assert list(ReviewRequestPost.objects.values_list("slack_thread_ts", flat=True)) == [self.slack.posts[0][2]]

    def test_an_unposted_claim_is_not_a_request_and_gets_sent(self) -> None:
        _ready_ticket()
        ReviewRequestPost.objects.create(
            mr_url=_URL,
            overlay=_OVERLAY,
            slack_channel_id=_CHANNEL,
            slack_thread_ts="",
            created_at=timezone.now() - dt.timedelta(days=2),
        )

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert _kinds(signals) == ["review_request.sent"]
        assert list(ReviewRequestPost.objects.values_list("slack_thread_ts", flat=True)) == [self.slack.posts[0][2]]

    def test_one_merge_request_raising_does_not_stop_the_next(self) -> None:
        self.forge.my_prs = [_mr(1), _mr(2)]
        for iid in (1, 2):
            _ready_ticket(iid=iid)

        with _world(self.slack, self.forge):
            signals = self._scanner(_FailingFirstPoster(_mr(1)["web_url"])).scan()

        assert _verdicts(signals)[0] == ("review_request.send_deferred", "error:RuntimeError")
        assert _kinds(signals)[1] == "review_request.sent"
        assert [_mr(2)["web_url"] in text for _channel, text, _ts in self.slack.posts] == [True]


class TestTheLiveHeadIsReadBeforeSending(_SenderCase):
    def test_a_head_that_moved_since_the_listing_is_deferred(self) -> None:
        _ready_ticket()
        self.forge.live_head = _NEW_HEAD

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert _verdicts(signals) == [("review_request.send_deferred", "head_moved")]

    def test_an_unreadable_live_head_is_deferred(self) -> None:
        _ready_ticket()
        self.forge.live_head = HEAD_SHA_UNREADABLE

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert _verdicts(signals) == [("review_request.send_deferred", "head_unreadable")]


class TestTheOwnersAnswerBindsToTheHead(_SenderCase):
    def _ask_at_head(self, *, attested: bool = True) -> Ticket:
        ticket = _ready_ticket(verdict_at=_OLD_HEAD, attested=attested)
        with _world(self.slack, self.forge):
            _followup_pass(self.forge, self.slack)
        return ticket

    def test_a_refusal_after_the_owner_said_post_asks_nothing_more_at_that_head(self) -> None:
        self._ask_at_head(attested=False)
        _answer(_POST)

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert _verdicts(signals) == [("review_request.send_refused", "anti_vacuity_not_attested")]
        assert signals[0].payload["asked"] is False
        assert DeferredQuestion.objects.filter(dedupe_marker=_ASKED_AT_HEAD).count() == 1

    def test_a_decline_holds_the_send_even_after_the_review_lands(self) -> None:
        ticket = self._ask_at_head()
        _answer(_NOT_READY)
        ReviewVerdict.record(
            pr_id=7, slug=_SLUG, reviewed_sha=_HEAD, verdict="merge_safe", reviewer_identity="cold", ticket=ticket
        )

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert _verdicts(signals) == [("review_request.send_deferred", "owner_declined")]
        rows = DeferredQuestion.objects.filter(dedupe_marker=_ASKED_AT_HEAD)
        assert (rows.filter(answered_at__isnull=False).count(), _mr_state_questions()) == (1, [])

    def test_a_new_head_reopens_the_decision(self) -> None:
        ticket = self._ask_at_head()
        _answer(_IN_PERSON)
        self.forge.my_prs = [_mr(sha=_NEW_HEAD)]
        ticket.record_anti_vacuity_attestation(_NEW_HEAD, "ACs checked against diff", [], no_new_tests=True)
        ReviewVerdict.record(
            pr_id=7, slug=_SLUG, reviewed_sha=_NEW_HEAD, verdict="merge_safe", reviewer_identity="cold", ticket=ticket
        )

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert _kinds(signals) == ["review_request.sent"]
        assert len(self.slack.posts) == 1

    def test_a_post_answer_waives_the_missing_cold_review(self) -> None:
        self._ask_at_head()
        _answer(_POST)

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert _kinds(signals) == ["review_request.sent"]
        assert len(self.slack.posts) == 1

    def test_a_post_answer_never_overrides_a_hold(self) -> None:
        ticket = self._ask_at_head()
        _answer(_POST)
        ReviewVerdict.record(
            pr_id=7, slug=_SLUG, reviewed_sha=_HEAD, verdict="hold", reviewer_identity="cold", ticket=ticket
        )

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert _verdicts(signals) == [("review_request.send_deferred", "hold_at_head")]

    def test_a_question_about_an_old_head_is_replaced_by_one_about_the_new_head(self) -> None:
        self._ask_at_head()
        old = _pending_question()
        self.forge.my_prs = [_mr(sha=_NEW_HEAD)]

        with _world(self.slack, self.forge):
            _followup_pass(self.forge, self.slack)

        old.refresh_from_db()
        assert old.dismissed_at is not None
        assert head_tag(_NEW_HEAD) in _pending_question().question

    def test_another_callers_open_question_is_kept_and_counts_as_asked(self) -> None:
        batch_gate_row = ask_mr_state(mr_url=_URL, reason="its work group is not ready.")
        assert batch_gate_row is not None
        _ready_ticket(verdict_at=_OLD_HEAD)

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert _pending_question().pk == batch_gate_row.pk
        assert signals[0].payload["asked"] is True


class TestAnAnswerToTheSurveyorBindsNoSend(_SenderCase):
    """The surveyor's question is internal (#5096), so only the owner's answer to the sender's own question binds."""

    def _surveyor_asks_and_it_is_answered(self, answer: str) -> None:
        seed_forbidding_posture()
        with _world(self.slack, self.forge):
            _ship_triage_pass(self.forge, self.slack)
        _answer(answer)
        seed_permitting_posture()

    def test_a_decline_to_the_surveyor_does_not_hold_a_reviewed_send(self) -> None:
        _ready_ticket()
        self._surveyor_asks_and_it_is_answered(_NOT_READY)

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert _kinds(signals) == ["review_request.sent"]

    def test_a_post_answer_to_the_surveyor_asks_the_owner_instead_of_sending(self) -> None:
        _ready_ticket(verdict_at=_OLD_HEAD)
        self._surveyor_asks_and_it_is_answered(_POST)

        with _world(self.slack, self.forge):
            signals = _followup_pass(self.forge, self.slack)

        assert self.slack.posts == []
        assert _verdicts(signals) == [("review_request.send_refused", "awaiting_cold_review")]
        assert _pending_question().audience == DeferredQuestion.Audience.OWNER_QUESTION

    def test_the_surveyor_asks_nothing_more_at_an_answered_head(self) -> None:
        seed_forbidding_posture()
        with _world(self.slack, self.forge):
            _ship_triage_pass(self.forge, self.slack)
            _answer(_IN_PERSON)
            _ship_triage_pass(self.forge, self.slack)

        assert _mr_state_questions() == []


class TestTheOwnerQuestionLivesWhereTheSenderIsAbsent(_SenderCase):
    """The triage surveyor still asks when no sender can act on its verdict."""

    def _ship_triage_scan(self) -> list[ScanSignal]:
        return _ship_triage_pass(self.forge, self.slack)

    def test_a_permitting_posture_with_followup_running_leaves_the_ask_to_the_sender(self) -> None:
        _admit_followup(runs=True)

        with _world(self.slack, self.forge):
            self._ship_triage_scan()

        assert _mr_state_questions() == []

    def test_a_followup_loop_that_will_not_run_still_asks_the_owner(self) -> None:
        _admit_followup(runs=False)

        with _world(self.slack, self.forge):
            self._ship_triage_scan()

        assert _mr_state_questions() == [_ASKED_AT_HEAD]

    def test_a_forbidding_posture_still_asks_the_owner(self) -> None:
        _admit_followup(runs=True)
        seed_forbidding_posture()

        with _world(self.slack, self.forge):
            self._ship_triage_scan()

        assert _mr_state_questions() == [_ASKED_AT_HEAD]

    def test_no_messaging_backend_still_asks_the_owner(self) -> None:
        _admit_followup(runs=True)

        with _world(self.slack, self.forge):
            _ship_triage_pass(self.forge, None)

        assert _mr_state_questions() == [_ASKED_AT_HEAD]


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

    def test_refused_invocations_do_not_use_up_the_pass(self) -> None:
        self.forge.my_prs = [_mr(iid) for iid in (1, 2, 3, 4)]
        for iid in (1, 2, 3):
            _ready_ticket(iid=iid, attested=False)
        _ready_ticket(iid=4)

        with _world(self.slack, self.forge):
            _followup_pass(self.forge, self.slack)

        assert [_mr(4)["web_url"] in text for _channel, text, _ts in self.slack.posts] == [True]


class TestAnUnreadableAnswerIsNeverTrusted(_SenderCase):
    def test_a_command_that_prints_no_verdict_is_refused_to_the_owner(self) -> None:
        _ready_ticket()

        def _prose_only(*_args: object, stream: io.StringIO) -> None:
            stream.write("Usage: review_request_post [OPTIONS]\n")
            raise SystemExit(2)

        with (
            _world(self.slack, self.forge),
            patch("teatree.loop.scanners.review_request_send.call_command_streamed", _prose_only),
        ):
            signals = self._scanner().scan()

        assert _verdicts(signals) == [("review_request.send_refused", "unreadable_result")]
        assert self.slack.posts == []

    def test_a_payload_with_no_head_sha_is_deferred(self) -> None:
        _ready_ticket()
        self.forge.my_prs = [_mr(sha="")]

        with _world(self.slack, self.forge):
            signals = self._scanner().scan()

        assert _verdicts(signals) == [("review_request.send_deferred", "head_unreadable")]
        assert self.slack.posts == []


class TestTheFollowupPreviewSendsNothing(TransactionTestCase):
    def test_the_preview_reports_the_owed_request_and_makes_none_of_it(self) -> None:
        seed_permitting_posture()
        allow_slack_channels(_CHANNEL)
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
        assert actions.count(("review_request_post", "posted")) == 1, report.candidates
        assert _SENDER in report.selected_scanners
        assert report.breach_count == 0
        assert report.errors == {}
        assert slack.posts == []
        assert persisted == []
        assert not ReviewRequestPost.objects.exists()
