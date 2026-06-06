"""Per-item fault isolation across loop scanners (#1597).

Each test verifies that when one item in a scanner's loop raises an
unexpected exception, sibling items still produce their signals. A test
written against the unfixed code is first confirmed RED (the second item's
signal is missing), then becomes GREEN after the per-item try/except guard
is added.

Covered scanners:
- ReviewerPrsScanner (main PR loop + orphan ticket loop)
- ActiveTicketsScanner
- SlackBroadcastsScanner (per-message, with ConnectChannelBotRestrictedError escalation)
- TicketCompletionScanner
- RedCardScanner (reaction loop + DM loop)
- SlackReviewIntentScanner (reaction loop + mention loop)
- TodoSweepScanner
- IssueImplementerScanner
- SlackDmInboundScanner
"""

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState, ReviewState
from teatree.core.models import ImplementedIssueMarker
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.overlay import OverlayBase
from teatree.loop.scanners.active_tickets import ActiveTicketsScanner
from teatree.loop.scanners.issue_implementer import IssueImplementerScanner
from teatree.loop.scanners.red_card import RedCardScanner
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.loop.scanners.slack_broadcasts import ConnectChannelBotRestrictedError, MrState, SlackBroadcastsScanner
from teatree.loop.scanners.slack_dm_inbound import SlackDmInboundScanner
from teatree.loop.scanners.slack_review_intent import SlackReviewIntentScanner
from teatree.loop.scanners.ticket_completion import TicketCompletionScanner
from teatree.loop.scanners.todo_sweep import TodoSweepScanner
from teatree.types import RawAPIDict
from tests.teatree_core._on_behalf_gate_helpers import disable_on_behalf_gate


@pytest.fixture(autouse=True)
def _gate_off(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    disable_on_behalf_gate(tmp_path_factory, monkeypatch)


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeCodeHost:
    user: str = "alice"
    my_prs: list[RawAPIDict] = field(default_factory=list)
    review_requested_prs: list[RawAPIDict] = field(default_factory=list)
    assigned_issues: list[RawAPIDict] = field(default_factory=list)
    review_state_by_url: dict[str, ReviewState] = field(default_factory=dict)
    pr_open_state_by_url: dict[str, PrOpenState] = field(default_factory=dict)
    pr_open_state_default: PrOpenState = PrOpenState.UNKNOWN
    raise_on_pr_url: str = ""
    raise_on_open_state_url: str = ""

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return self.my_prs

    def list_review_requested_prs(self, *, reviewer: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (reviewer, updated_after)
        return self.review_requested_prs

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
        return self.assigned_issues

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        _ = reviewer
        if pr_url == self.raise_on_pr_url:
            msg = "simulated network failure"
            raise RuntimeError(msg)
        return self.review_state_by_url.get(pr_url, ReviewState.NONE)

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        if pr_url == self.raise_on_open_state_url:
            msg = "simulated network failure"
            raise RuntimeError(msg)
        return self.pr_open_state_by_url.get(pr_url, self.pr_open_state_default)

    def create_pr(self, spec: Any) -> RawAPIDict:
        _ = spec
        return {}

    def post_pr_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict:
        _ = (repo, pr_iid, body)
        return {}

    def update_pr_comment(self, *, repo: str, pr_iid: int, comment_id: int, body: str) -> RawAPIDict:
        _ = (repo, pr_iid, comment_id, body)
        return {}

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        _ = (repo, pr_iid)
        return []

    def upload_file(self, *, repo: str, filepath: str) -> RawAPIDict:
        _ = (repo, filepath)
        return {}

    def get_issue(self, issue_url: str) -> RawAPIDict:
        _ = issue_url
        return {}


@dataclass
class _FakeMessaging:
    user_id: str = "U0DEMOUSER1"
    reactions: list[RawAPIDict] = field(default_factory=list)
    mentions: list[RawAPIDict] = field(default_factory=list)
    dms: list[RawAPIDict] = field(default_factory=list)
    messages_by_ts: dict[tuple[str, str], RawAPIDict] = field(default_factory=dict)
    react_calls: list[tuple[str, str, str]] = field(default_factory=list)
    react_raises: BaseException | None = None

    def fetch_mentions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        events, self.mentions = self.mentions, []
        return events

    def fetch_dms(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        events, self.dms = self.dms, []
        return events

    def fetch_reactions(self, *, since: str = "") -> list[RawAPIDict]:
        _ = since
        events, self.reactions = self.reactions, []
        return events

    def fetch_message(self, *, channel: str, ts: str) -> RawAPIDict:
        return self.messages_by_ts.get((channel, ts), {})

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
        _ = (channel, ts)
        return ""

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        if self.react_raises is not None:
            raise self.react_raises
        self.react_calls.append((channel, ts, emoji))
        return {"ok": True}

    def react_routed(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        return self.react(channel=channel, ts=ts, emoji=emoji)

    def resolve_user_id(self, handle: str) -> str:
        _ = handle
        return ""

    def auth_test(self) -> RawAPIDict:
        return {"ok": True}


# ---------------------------------------------------------------------------
# ReviewerPrsScanner — main PR loop
# ---------------------------------------------------------------------------

CHANNEL = "C0DEMOCHAN1"
URL_A = "https://gitlab.example.com/team/project/-/merge_requests/100"
URL_B = "https://gitlab.example.com/team/project/-/merge_requests/101"


class TestReviewerPrsIsolation(TestCase):
    """Sibling PR survives when _signals_for_pr raises on the first PR."""

    def _pr(self, url: str, author: str = "bob") -> RawAPIDict:
        return {"web_url": url, "sha": "abc", "author": {"username": author}, "state": "opened"}

    def test_failing_first_pr_does_not_suppress_second_pr_signal(self) -> None:
        host = _FakeCodeHost(
            user="alice",
            review_requested_prs=[self._pr(URL_A), self._pr(URL_B)],
        )
        scanner = ReviewerPrsScanner(host=host)

        call_count = [0]
        original_signals_for_pr = ReviewerPrsScanner._signals_for_pr

        def _raising_signals_for_pr(self_inner, pr: RawAPIDict, url: str, *args: Any, **kwargs: Any) -> list:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "simulated failure for first PR"
                raise RuntimeError(msg)
            return original_signals_for_pr(self_inner, pr, url, *args, **kwargs)

        with patch.object(ReviewerPrsScanner, "_signals_for_pr", _raising_signals_for_pr):
            signals = scanner.scan()

        kinds = [s.kind for s in signals]
        assert any("review" in k for k in kinds), "second PR must emit a signal even though the first PR raised"
        urls = [s.payload.get("mr_url") or s.payload.get("url", "") for s in signals]
        assert not any(URL_A in u for u in urls), "first (failing) PR must not produce a signal"
        assert any(URL_B in u for u in urls), "second (healthy) PR must produce its signal"


# ---------------------------------------------------------------------------
# ReviewerPrsScanner — orphaned ticket loop
# ---------------------------------------------------------------------------


class TestReviewerPrsOrphanIsolation(TestCase):
    """Sibling orphaned ticket survives when the first ticket's get_pr_open_state raises."""

    def test_failing_first_ticket_does_not_suppress_second_orphan_signal(self) -> None:
        ticket_a = Ticket.objects.create(overlay="acme", issue_url=URL_A, role="reviewer")
        ticket_b = Ticket.objects.create(overlay="acme", issue_url=URL_B, role="reviewer")
        session_a = Session.objects.create(overlay="acme", ticket=ticket_a, agent_id="a")
        session_b = Session.objects.create(overlay="acme", ticket=ticket_b, agent_id="b")
        Task.objects.create(ticket=ticket_a, session=session_a, phase="reviewing")
        Task.objects.create(ticket=ticket_b, session=session_b, phase="reviewing")

        host = _FakeCodeHost(
            user="alice",
            raise_on_open_state_url=URL_A,
            pr_open_state_by_url={URL_B: PrOpenState.MERGED},
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()
        orphan_signals = [s for s in signals if s.kind == "reviewer_pr.task_orphaned"]
        assert len(orphan_signals) == 1, "second ticket must be reaped even though first raised"
        assert orphan_signals[0].payload["url"] == URL_B


# ---------------------------------------------------------------------------
# ActiveTicketsScanner
# ---------------------------------------------------------------------------


class TestActiveTicketsIsolation(TestCase):
    """Sibling ticket still emits signal when processing the first ticket raises."""

    def test_failing_first_ticket_does_not_suppress_second_ticket_signal(self) -> None:
        ticket_a = Ticket.objects.create(overlay="acme", issue_url="https://x/1", state="started")
        Ticket.objects.create(overlay="acme", issue_url="https://x/2", state="coded")

        # Make _enqueue_short_describe raise on ticket_a by giving it a cached
        # title (triggering the enqueue path) and making Session.objects.create raise.
        Ticket.objects.filter(pk=ticket_a.pk).update(
            extra={"issue_title": "something"},
        )

        original_create = Session.objects.create
        call_count = [0]

        def _raising_create(**kwargs: Any) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "simulated DB error on first ticket"
                raise RuntimeError(msg)
            return original_create(**kwargs)

        with patch("teatree.core.models.session.Session.objects.create", side_effect=_raising_create):
            signals = ActiveTicketsScanner().scan()

        assert len(signals) == 1, "second ticket must emit its signal even though first raised"
        assert signals[0].payload["state"] == "coded"


# ---------------------------------------------------------------------------
# SlackBroadcastsScanner — per-message isolation + ConnectChannelBotRestrictedError propagation
# ---------------------------------------------------------------------------

MR_OPEN_A = "https://gitlab.example.com/team/project/-/merge_requests/200"
MR_OPEN_B = "https://gitlab.example.com/team/project/-/merge_requests/201"
TS_A = "1779201478.501469"
TS_B = "1779201499.123456"


def _fetcher(messages_by_channel: dict[str, list[RawAPIDict]]):
    def fetch(*, channel: str) -> list[RawAPIDict]:
        return list(messages_by_channel.get(channel, []))

    return fetch


def _classifier(states: dict[str, MrState]):
    def classify(urls: list[str]) -> list[MrState]:
        return [states[url] for url in urls]

    return classify


def _message(text: str, ts: str) -> RawAPIDict:
    return {"text": text, "ts": ts, "user": "USRG", "type": "message"}


class TestSlackBroadcastsMessageIsolation(TestCase):
    """Second message survives when _handle_message raises on the first."""

    def test_failing_first_message_does_not_suppress_second_message_signal(self) -> None:
        backend = _FakeMessaging()
        history = {
            CHANNEL: [
                _message(f"review {MR_OPEN_A}", TS_A),
                _message(f"review {MR_OPEN_B}", TS_B),
            ]
        }
        states = {
            MR_OPEN_A: MrState(url=MR_OPEN_A, merged=False, approved=False),
            MR_OPEN_B: MrState(url=MR_OPEN_B, merged=False, approved=False),
        }
        call_count = [0]
        original_handle = SlackBroadcastsScanner._handle_message

        def _raising_handle(self_inner, channel: str, message: RawAPIDict) -> list:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "simulated processing failure"
                raise RuntimeError(msg)
            return original_handle(self_inner, channel, message)

        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )
        with patch.object(SlackBroadcastsScanner, "_handle_message", _raising_handle):
            signals = scanner.scan()

        assert len(signals) >= 1, "second message must still produce its signal"
        assert any(s.payload.get("mr_url") == MR_OPEN_B for s in signals)


class TestSlackBroadcastsConnectChannelEscalation(TestCase):
    """ConnectChannelBotRestrictedError still propagates through the per-message guard."""

    def test_connect_channel_error_propagates_out_of_scan(self) -> None:
        # The scanner posts only the all-merged :white_check_mark: outcome
        # reaction now (no discovery-time :eyes:, #113/#86); a Connect-
        # restricted channel rejecting it must still propagate the escalation.
        backend = _FakeMessaging(react_raises=RuntimeError("Slack API not_in_channel"))
        history = {CHANNEL: [_message(f"{MR_OPEN_A}", TS_A)]}
        states = {MR_OPEN_A: MrState(url=MR_OPEN_A, merged=True, approved=True)}
        scanner = SlackBroadcastsScanner(
            backend=backend,
            channels=[CHANNEL],
            fetch_channel_history=_fetcher(history),
            classify_mrs=_classifier(states),
        )
        with pytest.raises(ConnectChannelBotRestrictedError):
            scanner.scan()


# ---------------------------------------------------------------------------
# TicketCompletionScanner
# ---------------------------------------------------------------------------


class _Overlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return ["acme-repo"]

    def get_provision_steps(self, worktree: Any) -> list:
        _ = worktree
        return []

    def is_issue_done(self, issue_data: dict[str, object]) -> bool:
        return issue_data.get("state") in {"closed", "completed", "merged"}


class TestTicketCompletionIsolation(TestCase):
    """Sibling ticket still emits when processing the first ticket raises."""

    def test_failing_first_ticket_does_not_suppress_second_ticket_completion(self) -> None:
        Ticket.objects.create(overlay="acme", issue_url="https://x/shipped/1", state="shipped")
        Ticket.objects.create(overlay="acme", issue_url="https://x/shipped/2", state="shipped")

        call_count = [0]

        def _patched_get_code_host(overlay: Any, url: str) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "simulated host lookup failure"
                raise RuntimeError(msg)
            host = _FakeCodeHost()
            host.get_issue = lambda issue_url: {"state": "closed"}  # type: ignore[assignment]
            return host

        scanner = TicketCompletionScanner(overlay=_Overlay(), overlay_name="acme")
        with patch("teatree.loop.scanners.ticket_completion.get_code_host_for_url", _patched_get_code_host):
            signals = scanner.scan()

        assert len(signals) == 1, "second ticket must still emit completion_detected"
        assert signals[0].kind == "ticket.completion_detected"


# ---------------------------------------------------------------------------
# RedCardScanner — reaction loop
# ---------------------------------------------------------------------------

REACTION_TS_A = "1779180557.000100"
REACTION_TS_B = "1779180558.000200"
RC_CHANNEL = "C09D25ZHCRJ"
DM_CHANNEL = "D0DEMOTEAM1"
USER = "U0DEMOUSER1"


def _reaction_event(ts: str, channel: str = RC_CHANNEL) -> RawAPIDict:
    return {
        "type": "reaction_added",
        "user": USER,
        "reaction": "red_circle",
        "item": {"type": "message", "channel": channel, "ts": ts},
        "event_ts": ts,
    }


def _dm_event(ts: str, text: str = "RED CARD") -> RawAPIDict:
    return {"ts": ts, "text": text, "channel": DM_CHANNEL, "user": USER}


class TestRedCardReactionIsolation(TestCase):
    """Second reaction event still produces a signal when the first raises."""

    def test_failing_first_reaction_does_not_suppress_second_reaction_signal(self) -> None:
        backend = _FakeMessaging(
            user_id=USER,
            reactions=[_reaction_event(REACTION_TS_A), _reaction_event(REACTION_TS_B)],
            messages_by_ts={
                (RC_CHANNEL, REACTION_TS_A): {"text": "agent msg a"},
                (RC_CHANNEL, REACTION_TS_B): {"text": "agent msg b"},
            },
        )
        scanner = RedCardScanner(backend=backend, overlay="acme")

        call_count = [0]
        original_handle = RedCardScanner._handle_reaction

        def _raising_handle(self_inner, event: RawAPIDict, target_user: str) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "simulated failure on reaction 1"
                raise RuntimeError(msg)
            return original_handle(self_inner, event, target_user)

        with patch.object(RedCardScanner, "_handle_reaction", _raising_handle):
            signals = scanner.scan()

        assert len(signals) == 1, "second reaction must still produce its signal"
        assert signals[0].kind == "red_card.signal"


class TestRedCardDmIsolation(TestCase):
    """Second DM event still produces a signal when the first raises."""

    def test_failing_first_dm_does_not_suppress_second_dm_signal(self) -> None:
        backend = _FakeMessaging(
            user_id=USER,
            dms=[_dm_event("1779.001"), _dm_event("1779.002")],
        )
        scanner = RedCardScanner(backend=backend, overlay="acme")

        call_count = [0]
        original_handle = RedCardScanner._handle_dm

        def _raising_handle(self_inner, event: RawAPIDict, target_user: str) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "simulated failure on DM 1"
                raise RuntimeError(msg)
            return original_handle(self_inner, event, target_user)

        with patch.object(RedCardScanner, "_handle_dm", _raising_handle):
            signals = scanner.scan()

        assert len(signals) == 1, "second DM must still produce its signal"
        assert signals[0].kind == "red_card.signal"


# ---------------------------------------------------------------------------
# SlackReviewIntentScanner — reaction + mention loops
# ---------------------------------------------------------------------------

MR_URL = "https://gitlab.com/owner/repo/-/merge_requests/42"
RI_TS_A = "1779180558.000300"
RI_TS_B = "1779180558.000400"
RI_CHANNEL = "C0REVIEW"


def _review_reaction(ts: str) -> RawAPIDict:
    return {
        "type": "reaction_added",
        "user": USER,
        "reaction": "eyes",
        "item": {"type": "message", "channel": RI_CHANNEL, "ts": ts},
        "event_ts": ts,
    }


def _mention_event(ts: str) -> RawAPIDict:
    return {"ts": ts, "text": f"<@{USER}> please review {MR_URL}", "channel": RI_CHANNEL}


class TestSlackReviewIntentReactionIsolation(TestCase):
    """Second reaction still produces a signal when the first raises."""

    def test_failing_first_reaction_does_not_suppress_second_reaction_signal(self) -> None:
        backend = _FakeMessaging(
            user_id=USER,
            reactions=[_review_reaction(RI_TS_A), _review_reaction(RI_TS_B)],
            messages_by_ts={
                (RI_CHANNEL, RI_TS_A): {"text": f"review {MR_URL}"},
                (RI_CHANNEL, RI_TS_B): {"text": f"review {MR_URL}"},
            },
        )
        scanner = SlackReviewIntentScanner(backend=backend, overlay="acme")

        call_count = [0]
        original_handle = SlackReviewIntentScanner._handle_reaction

        def _raising_handle(self_inner, event: RawAPIDict, target_user: str) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "simulated failure"
                raise RuntimeError(msg)
            return original_handle(self_inner, event, target_user)

        with (
            patch.object(SlackReviewIntentScanner, "_handle_reaction", _raising_handle),
            patch("teatree.backends.slack_receiver.drain_reactions_queue", return_value=[]),
        ):
            signals = scanner.scan()

        assert len(signals) == 1, "second reaction must still produce its signal"


class TestSlackReviewIntentMentionIsolation(TestCase):
    """Second mention still produces a signal when the first raises."""

    def test_failing_first_mention_does_not_suppress_second_mention_signal(self) -> None:
        backend = _FakeMessaging(
            user_id=USER,
            mentions=[_mention_event(RI_TS_A), _mention_event(RI_TS_B)],
        )
        scanner = SlackReviewIntentScanner(backend=backend, overlay="acme")

        call_count = [0]
        original_handle = SlackReviewIntentScanner._handle_mention

        def _raising_handle(self_inner, event: RawAPIDict, target_user: str) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "simulated failure"
                raise RuntimeError(msg)
            return original_handle(self_inner, event, target_user)

        with (
            patch.object(SlackReviewIntentScanner, "_handle_mention", _raising_handle),
            patch("teatree.backends.slack_receiver.drain_reactions_queue", return_value=[]),
        ):
            signals = scanner.scan()

        assert len(signals) == 1, "second mention must still produce its signal"


# ---------------------------------------------------------------------------
# TodoSweepScanner
# ---------------------------------------------------------------------------


class _TodoOverlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree: Any) -> list:
        _ = worktree
        return []

    def is_issue_done(self, issue_data: dict[str, object]) -> bool:
        return issue_data.get("state") == "closed"


URL_SWEEP_A = "https://example.com/issues/sweep/1"
URL_SWEEP_B = "https://example.com/issues/sweep/2"


class TestTodoSweepIsolation(TestCase):
    """Second task still produces a signal when _verify raises on the first."""

    def test_failing_first_task_does_not_suppress_second_task_signal(self) -> None:
        overlay = _TodoOverlay()
        ticket_a = Ticket.objects.create(overlay="acme", issue_url=URL_SWEEP_A)
        ticket_b = Ticket.objects.create(overlay="acme", issue_url=URL_SWEEP_B)
        session_a = Session.objects.create(overlay="acme", ticket=ticket_a, agent_id="a")
        session_b = Session.objects.create(overlay="acme", ticket=ticket_b, agent_id="b")
        Task.objects.create(ticket=ticket_a, session=session_a, phase="coding")
        Task.objects.create(ticket=ticket_b, session=session_b, phase="coding")

        scanner = TodoSweepScanner(overlay=overlay, overlay_name="acme")

        call_count = [0]
        original_verify = TodoSweepScanner._verify

        def _raising_verify(self_inner, task: Any) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "simulated verify failure"
                raise RuntimeError(msg)
            return original_verify(self_inner, task)

        host_b = _FakeCodeHost()
        host_b.get_issue = lambda url: {"state": "closed"}  # type: ignore[assignment]

        with (
            patch.object(TodoSweepScanner, "_verify", _raising_verify),
            patch("teatree.loop.scanners.todo_sweep.get_code_host_for_url", return_value=host_b),
        ):
            signals = scanner.scan()

        assert len(signals) == 1, "second task must still produce its signal"


# ---------------------------------------------------------------------------
# IssueImplementerScanner
# ---------------------------------------------------------------------------


class _ImplementerHost:
    user: str = "alice"
    issues: list[RawAPIDict]

    def __init__(self, issues: list[RawAPIDict]) -> None:
        self.issues = issues

    def current_user(self) -> str:
        return self.user

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
        return self.issues


IMPL_LABEL = "auto-implement"
IMPL_URL_A = "https://github.com/acme/repo/issues/1"
IMPL_URL_B = "https://github.com/acme/repo/issues/2"


class TestIssueImplementerIsolation(TestCase):
    def _issue(self, url: str) -> RawAPIDict:
        return {"web_url": url, "title": "do it", "labels": [IMPL_LABEL], "state": "open"}

    def test_failing_first_issue_does_not_suppress_second_issue_signal(self) -> None:
        host = _ImplementerHost(issues=[self._issue(IMPL_URL_A), self._issue(IMPL_URL_B)])
        scanner = IssueImplementerScanner(host=host, label=IMPL_LABEL, overlay_name="acme")

        call_count = [0]
        original_claim = ImplementedIssueMarker.objects.claim

        def _raising_claim(url: str, *, overlay: str) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "simulated DB failure"
                raise RuntimeError(msg)
            return original_claim(url, overlay=overlay)

        with patch.object(ImplementedIssueMarker.objects, "claim", _raising_claim):
            signals = scanner.scan()

        assert len(signals) == 1, "second issue must still be claimed and emitted"
        assert signals[0].payload["url"] == IMPL_URL_B


# ---------------------------------------------------------------------------
# SlackDmInboundScanner
# ---------------------------------------------------------------------------


class TestSlackDmInboundIsolation(TestCase):
    """Second DM still produces a signal when processing the first raises."""

    def test_failing_first_dm_does_not_suppress_second_dm_signal(self) -> None:
        dm_ts_a = "1779180560.000100"
        dm_ts_b = "1779180560.000200"
        dm_ch = "D0DIRECT"

        backend = _FakeMessaging(
            user_id=USER,
            dms=[
                {"ts": dm_ts_a, "text": "hello agent", "channel": dm_ch, "user": USER},
                {"ts": dm_ts_b, "text": "another message", "channel": dm_ch, "user": USER},
            ],
        )
        scanner = SlackDmInboundScanner(backend=backend, overlay="acme")

        call_count = [0]
        from teatree.core.models.pending_chat_injection import PendingChatInjection  # noqa: PLC0415

        original_record = PendingChatInjection.record

        def _raising_record(**kwargs: Any) -> Any:
            call_count[0] += 1
            if call_count[0] == 1:
                msg = "simulated record failure"
                raise RuntimeError(msg)
            return original_record(**kwargs)

        with (
            patch.object(PendingChatInjection, "record", _raising_record),
            patch(
                "teatree.loop.scanners.slack_dm_inbound.resolve_own_identity",
                return_value=None,
            ),
            patch(
                "teatree.loop.scanners.slack_dm_inbound.filter_self_messages",
                side_effect=lambda events, identity: events,
            ),
        ):
            signals = scanner.scan()

        assert len(signals) == 1, "second DM must still produce its signal"
        assert signals[0].payload["ts"] == dm_ts_b
