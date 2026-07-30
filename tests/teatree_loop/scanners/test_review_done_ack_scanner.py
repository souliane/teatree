"""The colleague-visible review-DONE ack fires off the FSM, not off a CLI call.

What was already there: the whole two-step review-pickup design
(:mod:`teatree.loop.review_claim`) — no ``:eyes:`` at discovery (#113/#86),
then ``:eyes:`` + a verdict emoji on the MR's Slack broadcast once the review
lands, deduped against existing reactors and the ``OutboundClaim`` ledger,
routed through the on-behalf egress. The Slack coordinates resolve from the
``ReviewRequestPost`` ledger, which the broadcast scanner already seeds for
every open MR it sees (#1256).

What was missing: the trigger. ``emit_review_done_reactions`` had exactly ONE
caller — ``review record``. A reviewer that posted its findings on the MR and
completed its task without running that command produced no ack at all. On
2026-07-22 four colleague MRs got real inline notes with no ``ReviewVerdict`` row
behind them, so the reaction path was never entered — upstream of any Slack
transport problem.

``ReviewDoneAckScanner`` rebinds the ack to the durable fact: a reviewer-role
ticket at ``REVIEW_POSTED`` has had its review posted. These tests drive the real
scanner and the real ``emit_review_done_reactions`` chokepoint with a fake
messaging backend — no colleague surface is touched.
"""

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

from django.test import TestCase

from teatree.core.models import ReviewRequestPost, ReviewVerdict
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loop.review_done_reactions import _egress_react, _slack_message_for_pr, emit_review_done_reactions
from teatree.loop.scanners.review_done_ack import ReviewDoneAckScanner
from teatree.types import RawAPIDict
from tests.teatree_core._on_behalf_gate_helpers import mode_immediate_cm

MR_URL = "https://gitlab.example.com/team/project/-/merge_requests/6613"
SLUG = "team/project"
CHANNEL = "C_REVIEW"
TS = "1784551462.298759"
HEAD_SHA = "c" * 40


@dataclass
class FakeMessaging:
    """Records reactions instead of posting them — no colleague surface is touched."""

    user_id: str = "U_SELF"
    reactions: list[tuple[str, str, str]] = field(default_factory=list)

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reactions.append((channel, ts, emoji))
        return {"ok": True}

    def react_routed(self, *, channel: str, ts: str, emoji: str, **_kwargs: Any) -> RawAPIDict:
        return self.react(channel=channel, ts=ts, emoji=emoji)

    @property
    def emojis(self) -> list[str]:
        return [emoji for _, _, emoji in self.reactions]


def _seed_reviewed_ticket(*, url: str = MR_URL, overlay: str = "team-overlay") -> Ticket:
    """A reviewer ticket whose reviewing task completed and which reached REVIEW_POSTED."""
    ticket = Ticket.objects.create(issue_url=url, overlay=overlay, role=Ticket.Role.REVIEWER)
    session = Session.objects.create(ticket=ticket, agent_id="external-review")
    Task.objects.create(
        ticket=ticket,
        session=session,
        phase="reviewing",
        status=Task.Status.COMPLETED,
        execution_target=Task.ExecutionTarget.HEADLESS,
    )
    Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.REVIEW_POSTED)
    ticket.refresh_from_db()
    return ticket


def _seed_broadcast_post(url: str = MR_URL) -> ReviewRequestPost:
    return ReviewRequestPost.objects.create(mr_url=url, slack_channel_id=CHANNEL, slack_thread_ts=TS)


class TestReviewDoneAckScanner(TestCase):
    def test_acks_a_completed_review_with_no_recorded_verdict(self) -> None:
        """The incident shape: findings posted on the MR, no ``ReviewVerdict`` recorded.

        Before this scanner the ONLY producer of the ack was ``review record``,
        so this case — the exact 2026-07-22 shape — emitted nothing and
        colleagues had no way to see the MR had been picked up.
        """
        _seed_reviewed_ticket()
        _seed_broadcast_post()
        messaging = FakeMessaging()

        with mode_immediate_cm():
            signals = ReviewDoneAckScanner(messaging=messaging, overlay_name="team-overlay").scan()

        assert messaging.emojis == ["eyes"]
        assert [signal.kind for signal in signals] == ["review_done_ack.reacted"]
        assert signals[0].payload["mr_url"] == MR_URL

    def test_uses_the_recorded_verdicts_emoji_set_when_one_exists(self) -> None:
        ticket = _seed_reviewed_ticket()
        _seed_broadcast_post()
        recorded = ReviewVerdict.record(
            pr_id=6613,
            slug=SLUG,
            reviewed_sha=HEAD_SHA,
            verdict="merge_safe",
            reviewer_identity="reviewer-bot",
            findings=[],
            blast_class="logic",
            gh_verify_result="green",
            ticket=ticket,
        )
        messaging = FakeMessaging()

        with mode_immediate_cm():
            ReviewDoneAckScanner(messaging=messaging, overlay_name="team-overlay").scan()

        assert messaging.emojis == list(recorded.done_reaction_emojis())

    def test_is_idempotent_across_ticks(self) -> None:
        """The ``OutboundClaim`` ledger makes a second tick a no-op."""
        _seed_reviewed_ticket()
        _seed_broadcast_post()
        messaging = FakeMessaging()
        scanner = ReviewDoneAckScanner(messaging=messaging, overlay_name="team-overlay")

        with mode_immediate_cm():
            scanner.scan()
            second = scanner.scan()

        assert messaging.emojis == ["eyes"]
        assert second == []

    def test_never_broadcast_mr_posts_nothing(self) -> None:
        """No tracked Slack message means there is nothing to react on."""
        _seed_reviewed_ticket()
        messaging = FakeMessaging()

        assert ReviewDoneAckScanner(messaging=messaging, overlay_name="team-overlay").scan() == []
        assert messaging.reactions == []

    def test_review_still_in_flight_is_not_acked(self) -> None:
        """The ack is a review-DONE signal — a ticket short of REVIEW_POSTED has not finished."""
        ticket = _seed_reviewed_ticket()
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.NOT_STARTED)
        _seed_broadcast_post()
        messaging = FakeMessaging()

        assert ReviewDoneAckScanner(messaging=messaging, overlay_name="team-overlay").scan() == []
        assert messaging.reactions == []

    def test_stale_review_outside_the_window_is_not_backfilled(self) -> None:
        """Enabling the scanner must not re-react across the whole review history."""
        _seed_reviewed_ticket()
        _seed_broadcast_post()
        messaging = FakeMessaging()

        scanner = ReviewDoneAckScanner(messaging=messaging, overlay_name="team-overlay", max_age_hours=0)

        assert scanner.scan() == []
        assert messaging.reactions == []

    def test_other_overlays_reviews_are_left_alone(self) -> None:
        _seed_reviewed_ticket(overlay="other-overlay")
        _seed_broadcast_post()
        messaging = FakeMessaging()

        assert ReviewDoneAckScanner(messaging=messaging, overlay_name="team-overlay").scan() == []
        assert messaging.reactions == []


class TestReviewDoneReactionBranches(TestCase):
    def test_emit_returns_empty_without_a_messaging_backend(self) -> None:
        assert emit_review_done_reactions(slug=SLUG, pr_id=6613, emojis=["eyes"], messaging=None) == []

    def test_egress_react_is_false_when_the_response_is_not_a_dict(self) -> None:
        egress = MagicMock()
        egress.react.return_value = "not-a-dict"
        assert _egress_react(egress, channel=CHANNEL, ts=TS, emoji="eyes", target_url=MR_URL) is False

    def test_slack_message_lookup_is_none_when_the_ledger_read_raises(self) -> None:
        _seed_broadcast_post()
        with patch("teatree.utils.url_slug.pr_ref_from_url", side_effect=RuntimeError("bad url")):
            assert _slack_message_for_pr(SLUG, 6613) is None


class TestReviewDoneAckResilience(TestCase):
    def test_ack_is_none_for_a_non_pr_ticket_url(self) -> None:
        ticket = Ticket.objects.create(issue_url="auto:branch", overlay="team-overlay", role=Ticket.Role.REVIEWER)
        scanner = ReviewDoneAckScanner(messaging=FakeMessaging(), overlay_name="team-overlay")
        assert scanner._ack(ticket) is None

    def test_ack_swallows_an_emit_failure(self) -> None:
        ticket = _seed_reviewed_ticket()
        scanner = ReviewDoneAckScanner(messaging=FakeMessaging(), overlay_name="team-overlay")
        with patch(
            "teatree.loop.scanners.review_done_ack.emit_review_done_reactions",
            side_effect=RuntimeError("slack down"),
        ):
            assert scanner._ack(ticket) is None
