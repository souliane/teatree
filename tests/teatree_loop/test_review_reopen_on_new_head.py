"""A reviewed colleague MR is re-reviewed when the author pushes a new head.

The incident (2026-07-22): the factory reviewed five colleague MRs, each
reviewer-role ticket short-circuited to REVIEW_POSTED, and the MRs were never
looked at again. On one such MR the author pushed 146 commits and GitLab reset
the approvals four hours later; nothing happened, because there was no live
ticket left and nothing watches a discharged review.

Three structural gaps compose into "review once, never again". Each is
pinned here.

Gap 1 — the broadcast review path records NO head SHA.
``SlackBroadcastsScanner`` discovers colleague MRs from a Slack review
channel and emits ``slack.review_intent``. That payload carried no
``head_sha``, so ``persistence._handle_reviewer`` seeded nothing and the
reviewer ticket's ``extra`` stayed ``{}`` — verified live on tickets
129/130/131/138/141. ``mark_reviewed_externally`` only stamps
``reviewed_sha``/``last_review_state`` when ``extra`` ALREADY carries a SHA,
so the whole at-head dedup machinery (``_already_reviewed_at_head``,
``ReviewerPrsScanner``'s cache) had nothing to key on. The MR-state
classifier already fetches the full forge JSON, so the SHA costs no extra
I/O.

Gap 2 — nothing re-opens a discharged review. ``ReviewerPrsScanner`` owns
the only ``reviewed_sha`` cache, but it lists ``list_review_requested_prs``
— a forge reviewer-*assignment* filter — so a Slack-broadcast MR that never
got a forge assignment is permanently absent from it (its own module says
so). ``ReviewedPrHeadScanner`` closes that: it watches reviewer tickets that
HAVE a recorded ``reviewed_sha`` and emits the existing
``reviewer_pr.new_sha`` when the live head moved past it.

Gap 3 — the FSM cannot complete a SECOND review. ``mark_reviewed_externally``
had no terminal state in its ``source=[...]``, so a re-review task on a
REVIEW_POSTED ticket completes without firing the transition and
``last_review_state`` is never re-stamped. ``_handle_reviewer`` has already
dropped the stale value by then, so the ticket falls out of the watch set for
good: the FIRST re-push would be reviewed and every later one silently
ignored — the same defect one push further along. The transition now
self-loops on REVIEW_POSTED, mirroring the #1431 fix to its sibling
``mark_review_no_action`` in the same file.

These tests drive the real scanner, the real dispatcher, the real
persistence entry point, and the real FSM transition, per the teatree
integration-test doctrine.
"""

from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState, ReviewState
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loop.dispatch import dispatch
from teatree.loop.persistence import _already_reviewed_at_head, persist_agent_actions
from teatree.loop.scanners.reviewed_pr_head import ReviewedPrHeadScanner
from teatree.loop.scanners.slack_broadcasts import MrState, SlackBroadcastsScanner
from teatree.types import RawAPIDict

OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
MR_URL = "https://gitlab.example.com/team/project/-/merge_requests/6613"


@dataclass
class FakeCodeHost:
    """In-memory ``CodeHostBackend`` covering the surface these scanners touch."""

    user: str = "reviewer-bot"
    live_head_by_url: dict[str, str] = field(default_factory=dict)
    pr_open_state_by_url: dict[str, PrOpenState] = field(default_factory=dict)
    pr_open_state_default: PrOpenState = PrOpenState.OPEN
    head_sha_calls: list[tuple[str, int]] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        self.head_sha_calls.append((slug, pr_id))
        for url, sha in self.live_head_by_url.items():
            if url.endswith(f"/{pr_id}") and slug.rsplit("/", maxsplit=1)[-1] in url:
                return sha
        return ""

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        return self.pr_open_state_by_url.get(pr_url, self.pr_open_state_default)

    def list_review_requested_prs(self, *, reviewer: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (reviewer, updated_after)
        return []

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        _ = (pr_url, reviewer)
        return ReviewState.NONE


@dataclass
class FakeMessagingBackend:
    """Minimal ``MessagingBackend`` — the broadcast scanner only reads ``user_id`` here."""

    user_id: str = "U_SELF"
    reactions: list[tuple[str, str, str]] = field(default_factory=list)

    def react(self, *, channel: str, ts: str, emoji: str) -> RawAPIDict:
        self.reactions.append((channel, ts, emoji))
        return {"ok": True}

    def react_routed(self, *, channel: str, ts: str, emoji: str, **_kwargs: Any) -> RawAPIDict:
        return self.react(channel=channel, ts=ts, emoji=emoji)


def _broadcast_message(text: str, ts: str = "1784551462.298759") -> RawAPIDict:
    return {"ts": ts, "text": text, "user": "U_COLLEAGUE"}


def _build_broadcast_scanner(*, head_sha: str) -> SlackBroadcastsScanner:
    """A broadcast scanner whose classifier reports one open colleague MR at *head_sha*."""
    return SlackBroadcastsScanner(
        backend=FakeMessagingBackend(),
        channels=["C_REVIEW"],
        fetch_channel_history=lambda *, channel: ([_broadcast_message(f"please review {MR_URL}")]),
        classify_mrs=lambda urls: [
            MrState(url=url, merged=False, approved=False, author_username="colleague", head_sha=head_sha)
            for url in urls
        ],
        overlay="team-overlay",
    )


def _seed_reviewed_ticket(
    *,
    state: str = Ticket.State.REVIEW_POSTED,
    reviewed_sha: str = OLD_SHA,
    last_review_state: str = ReviewState.APPROVED.value,
    url: str = MR_URL,
) -> Ticket:
    """A reviewer-role ticket whose review is discharged at *reviewed_sha*."""
    extra: dict[str, str] = {}
    if reviewed_sha:
        extra["reviewed_sha"] = reviewed_sha
    if last_review_state:
        extra["last_review_state"] = last_review_state
    ticket = Ticket.objects.create(issue_url=url, overlay="team-overlay", role=Ticket.Role.REVIEWER, extra=extra)
    Ticket.objects.filter(pk=ticket.pk).update(state=state)
    ticket.refresh_from_db()
    return ticket


def _seed_open_reviewing_task(ticket: Ticket) -> Task:
    session = Session.objects.create(ticket=ticket, agent_id="external-review")
    return Task.objects.create(
        ticket=ticket,
        session=session,
        phase="reviewing",
        status=Task.Status.PENDING,
        execution_target=Task.ExecutionTarget.HEADLESS,
    )


class TestGap1BroadcastCarriesHeadSha(TestCase):
    """The Slack-broadcast review path records the head SHA it dispatched at."""

    def test_review_intent_signal_carries_the_head_sha(self) -> None:
        """RED before the fix: the ``slack.review_intent`` payload has no ``head_sha``.

        Without it every downstream at-head check is inert, so a reviewed MR
        can never be told apart from a re-pushed one.
        """
        signals = _build_broadcast_scanner(head_sha=OLD_SHA).scan()

        intents = [signal for signal in signals if signal.kind == "slack.review_intent"]
        assert len(intents) == 1
        assert intents[0].payload.get("head_sha") == OLD_SHA

    def test_reviewer_ticket_is_seeded_with_the_reviewed_sha(self) -> None:
        """RED before the fix: the persisted reviewer ticket's ``extra`` stays ``{}``.

        Verified live on the incident's five reviewer tickets (129/130/131/
        138/141) — every one carried an empty ``extra``.
        """
        signals = _build_broadcast_scanner(head_sha=OLD_SHA).scan()
        persist_agent_actions(dispatch(signals))

        ticket = Ticket.objects.get(issue_url=MR_URL)
        assert (ticket.extra or {}).get("reviewed_sha") == OLD_SHA


class TestGap2ReviewedPrHeadScanner(TestCase):
    """A discharged review re-opens on a genuinely new head SHA — and only then."""

    def test_emits_new_sha_when_the_author_pushed(self) -> None:
        _seed_reviewed_ticket()
        host = FakeCodeHost(live_head_by_url={MR_URL: NEW_SHA})

        signals = ReviewedPrHeadScanner(host=host, overlay_name="team-overlay").scan()

        assert [signal.kind for signal in signals] == ["reviewer_pr.new_sha"]
        assert signals[0].payload["head_sha"] == NEW_SHA
        assert signals[0].payload["previous_sha"] == OLD_SHA

    def test_same_head_sha_emits_nothing(self) -> None:
        """Failure mode (b): re-reviewing the same tree on every tick."""
        _seed_reviewed_ticket()
        host = FakeCodeHost(live_head_by_url={MR_URL: OLD_SHA})

        assert ReviewedPrHeadScanner(host=host, overlay_name="team-overlay").scan() == []

    def test_open_reviewing_task_suppresses_the_signal(self) -> None:
        """Failure mode (a): a review already in flight must not be re-queued."""
        ticket = _seed_reviewed_ticket()
        _seed_open_reviewing_task(ticket)
        host = FakeCodeHost(live_head_by_url={MR_URL: NEW_SHA})

        assert ReviewedPrHeadScanner(host=host, overlay_name="team-overlay").scan() == []

    def test_never_reviewed_ticket_is_not_watched(self) -> None:
        """No recorded ``reviewed_sha`` means no discharged review to re-open."""
        _seed_reviewed_ticket(reviewed_sha="", last_review_state="")
        host = FakeCodeHost(live_head_by_url={MR_URL: NEW_SHA})

        scanner = ReviewedPrHeadScanner(host=host, overlay_name="team-overlay")

        assert scanner.scan() == []
        assert host.head_sha_calls == []

    def test_merged_pr_is_not_re_reviewed(self) -> None:
        _seed_reviewed_ticket()
        host = FakeCodeHost(
            live_head_by_url={MR_URL: NEW_SHA},
            pr_open_state_by_url={MR_URL: PrOpenState.MERGED},
        )

        assert ReviewedPrHeadScanner(host=host, overlay_name="team-overlay").scan() == []

    def test_unreadable_live_head_never_re_reviews(self) -> None:
        """An empty live head is "cannot confirm", never "the head moved"."""
        _seed_reviewed_ticket()

        assert ReviewedPrHeadScanner(host=FakeCodeHost(), overlay_name="team-overlay").scan() == []

    def test_other_overlays_tickets_are_left_alone(self) -> None:
        ticket = _seed_reviewed_ticket()
        Ticket.objects.filter(pk=ticket.pk).update(overlay="other-overlay")
        host = FakeCodeHost(live_head_by_url={MR_URL: NEW_SHA})

        assert ReviewedPrHeadScanner(host=host, overlay_name="team-overlay").scan() == []


class TestGap3ReReviewCompletesOnADeliveredTicket(TestCase):
    """The second review of the same MR must be able to finish — else it loops forever."""

    def test_re_review_re_arms_the_at_head_dedup(self) -> None:
        """RED before the fix: ``last_review_state`` is never re-stamped.

        ``Task.complete()`` guards the FSM advance on the transition's DERIVED
        source states. With REVIEW_POSTED absent the second review completes but
        the transition is skipped, so the reviewed-at record is left
        half-written and the ticket is never watched again — one more push and
        the factory is silent exactly as before.
        """
        ticket = _seed_reviewed_ticket()
        scanner = ReviewedPrHeadScanner(
            host=FakeCodeHost(live_head_by_url={MR_URL: NEW_SHA}),
            overlay_name="team-overlay",
        )
        actions = dispatch(scanner.scan())
        assert actions, "the scanner must produce a reviewer dispatch"
        assert actions[0].payload["url"] == MR_URL

        created = persist_agent_actions(actions)
        assert len(created) == 1, "a new head must schedule exactly one review task"

        created[0].complete()
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.REVIEW_POSTED
        assert (ticket.extra or {}).get("last_review_state") == ReviewState.APPROVED.value
        assert _already_reviewed_at_head(ticket, NEW_SHA) is True

    def test_no_second_task_while_the_head_is_unchanged(self) -> None:
        """The full loop is idempotent: a re-run at the same head schedules nothing."""
        _seed_reviewed_ticket()
        scanner = ReviewedPrHeadScanner(
            host=FakeCodeHost(live_head_by_url={MR_URL: NEW_SHA}),
            overlay_name="team-overlay",
        )
        first = persist_agent_actions(dispatch(scanner.scan()))
        assert len(first) == 1
        first[0].complete()

        stable = ReviewedPrHeadScanner(
            host=FakeCodeHost(live_head_by_url={MR_URL: NEW_SHA}),
            overlay_name="team-overlay",
        )

        assert stable.scan() == []
        assert persist_agent_actions(dispatch(stable.scan())) == []


class TestReviewedPrHeadScannerBranches(TestCase):
    """Direct coverage of the scanner's resilience + URL-claim branches."""

    def _scanner(self, host: object, **kwargs: object) -> ReviewedPrHeadScanner:
        return ReviewedPrHeadScanner(host=host, overlay_name="team-overlay", **kwargs)

    def test_scan_swallows_a_per_ticket_failure_and_continues(self) -> None:
        _seed_reviewed_ticket()

        class _BoomHost(FakeCodeHost):
            def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
                msg = "forge exploded"
                raise RuntimeError(msg)

        # the per-ticket exception is logged and skipped, not propagated
        assert self._scanner(_BoomHost()).scan() == []

    def test_signal_for_ticket_is_none_for_a_non_pr_url(self) -> None:
        ticket = Ticket.objects.create(issue_url="auto:some-branch", overlay="team-overlay", role=Ticket.Role.REVIEWER)
        assert self._scanner(FakeCodeHost())._signal_for_ticket(ticket) is None

    def test_pr_is_closed_is_false_when_the_state_read_raises(self) -> None:
        class _BoomStateHost(FakeCodeHost):
            def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
                msg = "state read failed"
                raise RuntimeError(msg)

        # UNKNOWN/unreadable keeps the review live — never a false "closed"
        assert self._scanner(_BoomStateHost())._pr_is_closed(MR_URL) is False

    def test_url_allowed_enforces_the_prefix_claim(self) -> None:
        scanner = self._scanner(FakeCodeHost(), allowed_url_prefixes=("https://gitlab.example.com/team/",))
        assert scanner._url_allowed(MR_URL) is True
        assert scanner._url_allowed("https://gitlab.example.com/other/project/-/merge_requests/1") is False
