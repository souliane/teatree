from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.review_request_post import ReviewRequestPost
from teatree.core.models.task import Task
from teatree.core.pr_inventory import PrInventoryEntry, PrInventorySnapshot
from teatree.core.review.mr_triage import RepoOwner
from teatree.dash.pr_board import PrBoard, PrBoardCard, build_pr_board
from tests.factories import PullRequestFactory, TaskAttemptFactory, TaskFactory, TicketFactory


def _group(board: PrBoard, name: str) -> list[str]:
    return [card.ref for group in board.groups if group.name == name for card in group.cards]


class PrBoardTestCase(TestCase):
    def setUp(self) -> None:
        inventory = patch("teatree.dash.pr_board.read_inventory", return_value=())
        inventory.start()
        self.addCleanup(inventory.stop)

    def test_owner_question_precedes_factory_task(self) -> None:
        ticket = TicketFactory()
        pr = PullRequestFactory(ticket=ticket)
        TaskFactory(ticket=ticket, phase="reviewing", status=Task.Status.PENDING)
        DeferredQuestion.objects.create(question="Approve the public post?", evidence={"pr_url": pr.url})

        board = build_pr_board()
        assert _group(board, "Needs you") == [f"{pr.repo}!{pr.iid}"]
        card = board.groups[0].cards[0]
        assert card.next_actor == "You"
        assert card.next_action == "Approve the public post?"

    def test_parked_task_owner_question_appears_on_its_pr(self) -> None:
        ticket = TicketFactory()
        pr = PullRequestFactory(ticket=ticket)
        task = TaskFactory(ticket=ticket, phase="shipping", status=Task.Status.PENDING)
        DeferredQuestion.objects.create(question="Choose the merge window?", parked_task=task)

        board = build_pr_board()
        assert _group(board, "Needs you") == [f"{pr.repo}!{pr.iid}"]
        assert board.groups[0].cards[0].next_action == "Choose the merge window?"

    def test_factory_task_and_waiting_review_are_separate(self) -> None:
        active_ticket = TicketFactory()
        active = PullRequestFactory(ticket=active_ticket)
        TaskFactory(ticket=active_ticket, phase="coding", status=Task.Status.CLAIMED)
        waiting = PullRequestFactory(ticket=TicketFactory())
        ReviewRequestPost.objects.create(mr_url=waiting.url, slack_channel_id="C1", slack_thread_ts="1.0")

        board = build_pr_board()
        assert _group(board, "Factory working") == [f"{active.repo}!{active.iid}"]
        assert _group(board, "Waiting on colleagues") == [f"{waiting.repo}!{waiting.iid}"]

    def _approved_green_pr(self) -> PullRequest:
        ticket = TicketFactory()
        pr = PullRequestFactory(ticket=ticket, state=PullRequest.State.APPROVED)
        ticket.extra = {
            "prs": {
                pr.url: {
                    "title": "Ready PR",
                    "pipeline_status": "success",
                    "approvals": {"count": 2, "required": 2},
                    "discussions": [],
                }
            }
        }
        ticket.save(update_fields=["extra"])
        return PullRequest.objects.get(pk=pr.pk)

    def _observed(self, pr: PullRequest, *, age: timedelta) -> tuple[PrInventorySnapshot, ...]:
        entry = PrInventoryEntry(
            url=pr.url,
            repo=pr.repo,
            iid=pr.iid,
            title="Ready PR",
            created_at="2026-10-01T10:00:00Z",
            updated_at="2026-10-02T10:00:00Z",
            next_task="Review or merge PR",
            decision="my_pr.open",
            ci="success",
            approvals="approved",
            threads="0 unresolved",
            draft=False,
        )
        return (PrInventorySnapshot(overlay=pr.overlay, observed_at=timezone.now() - age, entries=(entry,)),)

    def test_approved_green_pr_observed_by_the_last_tick_is_ready_to_merge(self) -> None:
        pr = self._approved_green_pr()

        with patch("teatree.dash.pr_board.read_inventory", return_value=self._observed(pr, age=timedelta(minutes=2))):
            card = next(card for group in build_pr_board().groups for card in group.cards)

        assert card.group == "Ready to merge or merging"
        assert (card.ci, card.approvals, card.threads) == ("success", "approved", "0 unresolved")
        assert card.title == "Ready PR"

    def test_approved_green_pr_with_a_stale_observation_is_blocked_not_ready(self) -> None:
        pr = self._approved_green_pr()

        with patch("teatree.dash.pr_board.read_inventory", return_value=self._observed(pr, age=timedelta(hours=2))):
            board = build_pr_board()
        card = next(card for group in board.groups for card in group.cards)

        assert card.group == "Blocked"
        assert card.next_actor == "Factory"
        assert "not verified" in card.next_action
        assert "stale" in board.inventory_warning

    def test_approved_green_pr_without_any_tick_observation_is_blocked_not_ready(self) -> None:
        self._approved_green_pr()

        card = next(card for group in build_pr_board().groups for card in group.cards)

        assert card.group == "Blocked"
        assert "not verified" in card.next_action

    def test_failed_task_is_blocked_and_unknown_facts_are_explicit(self) -> None:
        ticket = TicketFactory()
        PullRequestFactory(ticket=ticket)
        task = TaskFactory(ticket=ticket, phase="shipping", status=Task.Status.FAILED)
        TaskAttemptFactory(task=task, error="merge check failed")

        board = build_pr_board()
        card = next(card for group in board.groups for card in group.cards)
        assert card.group == "Blocked"
        assert "merge check failed" in card.next_action
        assert card.ci == "unknown"
        assert card.approvals == "unknown"
        assert card.threads == "unknown"

    def test_complete_snapshot_removes_a_closed_pr_record(self) -> None:
        PullRequestFactory(overlay="teatree")
        with patch(
            "teatree.dash.pr_board.read_inventory",
            return_value=(PrInventorySnapshot(overlay="teatree", observed_at=timezone.now(), entries=()),),
        ):
            board = build_pr_board()
        assert board.total == 0

    def test_stale_empty_snapshot_never_hides_an_open_recorded_pr(self) -> None:
        pr = PullRequestFactory(overlay="teatree")
        stale = PrInventorySnapshot(overlay="teatree", observed_at=timezone.now() - timedelta(days=3), entries=())
        with patch("teatree.dash.pr_board.read_inventory", return_value=(stale,)):
            board = build_pr_board()
        assert [card.ref for group in board.groups for card in group.cards] == [f"{pr.repo}!{pr.iid}"]
        assert "stale" in board.inventory_warning

    def test_fresh_snapshot_taken_before_the_row_was_verified_does_not_hide_it(self) -> None:
        taken = timezone.now() - timedelta(minutes=5)
        newer = PullRequestFactory(overlay="teatree", create_verified_at=taken + timedelta(minutes=2))
        PullRequestFactory(overlay="teatree", create_verified_at=taken - timedelta(minutes=2))
        snapshot = PrInventorySnapshot(overlay="teatree", observed_at=taken, entries=())
        with patch("teatree.dash.pr_board.read_inventory", return_value=(snapshot,)):
            board = build_pr_board()
        assert [card.ref for group in board.groups for card in group.cards] == [f"{newer.repo}!{newer.iid}"]

    def test_owned_snapshot_adds_unwatched_pr_without_erasing_sweep_verdict(self) -> None:
        def entry(url: str, decision: str, next_task: str) -> PrInventoryEntry:
            return PrInventoryEntry(
                url=url,
                repo="souliane/teatree",
                iid=int(url.rsplit("/", 1)[-1]),
                title="Owned PR",
                created_at="2026-10-01T10:00:00Z",
                updated_at="2026-10-02T10:00:00Z",
                next_task=next_task,
                decision=decision,
                ci="unknown",
                approvals="unknown",
                threads="unknown",
                draft=False,
            )

        shared = "https://github.com/souliane/teatree/pull/7"
        unwatched = "https://github.com/souliane/teatree/pull/8"
        snapshots = (
            PrInventorySnapshot(
                overlay="teatree",
                observed_at=timezone.now(),
                entries=(entry(shared, "flag_conflict", "Fix merge conflict"),),
            ),
            PrInventorySnapshot(
                overlay="own:teatree:github",
                observed_at=timezone.now(),
                entries=(entry(shared, "my_pr.open", "Inspect PR"), entry(unwatched, "my_pr.open", "Review PR")),
            ),
        )
        with patch("teatree.dash.pr_board.read_inventory", return_value=snapshots):
            board = build_pr_board()
        cards = {card.url: card for group in board.groups for card in group.cards}
        assert board.total == 2
        assert cards[shared].group == "Blocked"
        assert cards[shared].next_action == "Fix merge conflict"
        assert cards[unwatched].next_action == "Review PR"

    def test_fresh_red_ci_overrides_stale_green_ticket_cache(self) -> None:
        ticket = TicketFactory()
        pr = PullRequestFactory(ticket=ticket, state=PullRequest.State.APPROVED)
        ticket.extra = {"prs": {pr.url: {"pipeline_status": "success", "approvals": {"count": 2, "required": 2}}}}
        ticket.save(update_fields=["extra"])
        entry = PrInventoryEntry(
            url=pr.url,
            repo=pr.repo,
            iid=pr.iid,
            title="PR",
            created_at="2026-10-01T10:00:00Z",
            updated_at="2026-10-02T10:00:00Z",
            next_task="Fix failed check",
            decision="ci_red",
            ci="red",
            approvals="unknown",
            threads="unknown",
            draft=False,
        )
        snapshot = PrInventorySnapshot(overlay=pr.overlay, observed_at=timezone.now(), entries=(entry,))

        with patch("teatree.dash.pr_board.read_inventory", return_value=(snapshot,)):
            card = next(card for group in build_pr_board().groups for card in group.cards)
        assert card.ci == "red"
        assert card.group == "Blocked"
        assert "CI" in card.next_action

    def test_uncleared_pr_waits_for_review_instead_of_claiming_factory_work(self) -> None:
        url = "https://github.com/souliane/teatree/pull/98"
        entry = PrInventoryEntry(
            url=url,
            repo="souliane/teatree",
            iid=98,
            title="Awaiting review",
            created_at="2026-10-01T10:00:00Z",
            updated_at="2026-10-02T10:00:00Z",
            next_task="Obtain independent review and merge authorization",
            decision="skip",
            ci="success",
            approvals="unknown",
            threads="unknown",
            draft=False,
            reason="no_clear_for_head",
        )
        snapshot = PrInventorySnapshot(overlay="teatree", observed_at=timezone.now(), entries=(entry,))
        with patch("teatree.dash.pr_board.read_inventory", return_value=(snapshot,)):
            card = next(card for group in build_pr_board().groups for card in group.cards)
        assert card.group == "Waiting on colleagues"
        assert card.next_actor == "Reviewer"
        assert card.next_action == entry.next_task

    def test_reviewer_ticket_with_empty_cache_is_linked_by_pr_url(self) -> None:
        url = "https://github.com/souliane/teatree/pull/99"
        ticket = TicketFactory(issue_url=url, extra={})
        task = TaskFactory(ticket=ticket, phase="reviewing", status=Task.Status.PENDING)
        DeferredQuestion.objects.create(question="Can this review post go out?", parked_task=task)
        entry = PrInventoryEntry(
            url=url,
            repo="souliane/teatree",
            iid=99,
            title="Review PR",
            created_at="2026-10-01T10:00:00Z",
            updated_at="2026-10-02T10:00:00Z",
            next_task="Review PR",
            decision="my_pr.open",
            ci="success",
            approvals="unknown",
            threads="unknown",
            draft=False,
        )
        snapshot = PrInventorySnapshot(overlay="own:teatree:github", observed_at=timezone.now(), entries=(entry,))
        with patch("teatree.dash.pr_board.read_inventory", return_value=(snapshot,)):
            card = next(card for group in build_pr_board().groups for card in group.cards)
        assert card.ticket_id == ticket.pk
        assert card.group == "Needs you"

    def test_cache_only_closed_pr_is_not_presented_as_open(self) -> None:
        open_url = "https://gitlab.example/org/repo/-/merge_requests/41"
        closed_url = "https://gitlab.example/org/repo/-/merge_requests/42"
        TicketFactory(
            extra={
                "prs": {
                    open_url: {"state": "opened", "title": "Open MR"},
                    closed_url: {"state": "closed", "title": "Closed MR"},
                }
            }
        )

        board = build_pr_board()
        urls = {card.url for group in board.groups for card in group.cards}
        assert open_url in urls
        assert closed_url not in urls

    def test_unreadable_inventory_is_visible_and_recorded_pr_survives(self) -> None:
        pr = PullRequestFactory()
        with patch("teatree.dash.pr_board.read_inventory", side_effect=ValueError("bad inventory")):
            board = build_pr_board()
        assert "unavailable" in board.inventory_warning
        assert board.total == 1
        assert _group(board, "Blocked") == [f"{pr.repo}!{pr.iid}"]
        assert "unreadable" in board.groups[-1].cards[0].next_action


def _observed_entry(pr: PullRequest, **overrides: object) -> PrInventoryEntry:
    fields: dict[str, object] = {
        "url": pr.url,
        "repo": pr.repo,
        "iid": int(pr.iid),
        "title": "Observed PR",
        "created_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-02T10:00:00Z",
        "next_task": "Review or merge PR",
        "decision": "my_pr.open",
        "ci": "green",
        "approvals": "approved",
        "threads": "unknown",
        "draft": False,
    }
    return PrInventoryEntry(**{**fields, **overrides})


class PrBoardFreshObservationTestCase(TestCase):
    def setUp(self) -> None:
        inventory = patch("teatree.dash.pr_board.read_inventory", return_value=())
        inventory.start()
        self.addCleanup(inventory.stop)

    def _stale_ready_cache(self) -> PullRequest:
        ticket = TicketFactory()
        pr = PullRequestFactory(ticket=ticket, state=PullRequest.State.APPROVED)
        ticket.extra = {
            "prs": {pr.url: {"pipeline_status": "success", "approvals": {"count": 2, "required": 2}, "discussions": []}}
        }
        ticket.save(update_fields=["extra"])
        return PullRequest.objects.get(pk=pr.pk)

    def _card(self, pr: PullRequest, entry: PrInventoryEntry) -> PrBoardCard:
        snapshot = PrInventorySnapshot(overlay=pr.overlay, observed_at=timezone.now(), entries=(entry,))
        with patch("teatree.dash.pr_board.read_inventory", return_value=(snapshot,)):
            return next(card for group in build_pr_board().groups for card in group.cards)

    def test_fresh_observation_blocks_a_ready_claim_the_stale_cache_would_make(self) -> None:
        cases = {
            "draft": ("Blocked", {"reason": "draft", "ci": "unknown", "draft": True}),
            "changes requested": ("Blocked", {"reason": "changes_requested", "ci": "unknown"}),
            "CI pending": ("Factory working", {"reason": "ci_pending", "ci": "pending", "approvals": "unknown"}),
        }
        for name, (group, overrides) in cases.items():
            with self.subTest(name):
                pr = self._stale_ready_cache()
                card = self._card(pr, _observed_entry(pr, next_task=name, **overrides))
                assert card.group == group
                assert card.approvals != "2/2"

    def test_fresh_approved_green_observation_is_ready(self) -> None:
        pr = PullRequestFactory(ticket=TicketFactory())
        card = self._card(
            pr,
            _observed_entry(
                pr, reason="no_clear_for_head", next_task="Obtain independent review and merge authorization"
            ),
        )
        assert card.group == "Ready to merge or merging"
        assert (card.ci, card.approvals) == ("green", "approved")

    def test_approved_green_draft_is_not_ready(self) -> None:
        pr = PullRequestFactory(ticket=TicketFactory())
        card = self._card(pr, _observed_entry(pr, draft=True))
        assert card.draft
        assert card.group != "Ready to merge or merging"

    def test_green_pr_awaiting_approval_waits_on_the_reviewer(self) -> None:
        pr = PullRequestFactory(ticket=TicketFactory())
        card = self._card(pr, _observed_entry(pr, approvals="awaiting approval"))
        assert (card.group, card.next_actor) == ("Waiting on colleagues", "Reviewer")

    def test_green_own_pr_whose_approvals_are_unreadable_says_so_instead_of_naming_a_task(self) -> None:
        pr = PullRequestFactory(ticket=TicketFactory())
        card = self._card(pr, _observed_entry(pr, approvals="unknown"))
        assert (card.group, card.next_action) == ("Blocked", "Approvals unreadable; check the forge")

    def test_waiting_reviewer_thread_does_not_block_the_factory(self) -> None:
        ticket = TicketFactory()
        pr = PullRequestFactory(ticket=ticket)
        ticket.extra = {
            "prs": {pr.url: {"pipeline_status": "success", "discussions": [{"status": "waiting_reviewer"}]}}
        }
        ticket.save(update_fields=["extra"])
        card = next(card for group in build_pr_board().groups for card in group.cards)
        assert card.group != "Blocked"
        assert card.threads == "1 unresolved"


class PrBoardActionDetailsTestCase(TestCase):
    def setUp(self) -> None:
        inventory = patch("teatree.dash.pr_board.read_inventory", return_value=())
        inventory.start()
        self.addCleanup(inventory.stop)

    def test_red_ci_and_unresolved_threads_are_blocked_with_reasons(self) -> None:
        red_ticket = TicketFactory()
        red = PullRequestFactory(ticket=red_ticket)
        red_ticket.extra = {"prs": {red.url: {"pipeline_status": "failed"}}}
        red_ticket.save(update_fields=["extra"])
        thread_ticket = TicketFactory()
        threaded = PullRequestFactory(ticket=thread_ticket)
        thread_ticket.extra = {
            "prs": {threaded.url: {"pipeline_status": "success", "discussions": [{"status": "needs_reply"}]}}
        }
        thread_ticket.save(update_fields=["extra"])

        board = build_pr_board()
        cards = {card.url: card for group in board.groups for card in group.cards}
        assert cards[red.url].group == "Blocked"
        assert "CI" in cards[red.url].next_action
        assert cards[threaded.url].group == "Blocked"
        assert "thread" in cards[threaded.url].next_action

    def test_ready_requires_all_approvals_as_well_as_green_ci(self) -> None:
        ticket = TicketFactory()
        pr = PullRequestFactory(ticket=ticket, state=PullRequest.State.APPROVED)
        ticket.extra = {"prs": {pr.url: {"pipeline_status": "success", "approvals": {"count": 1, "required": 2}}}}
        ticket.save(update_fields=["extra"])

        card = next(card for group in build_pr_board().groups for card in group.cards)
        assert card.group == "Waiting on colleagues"
        assert "approval" in card.next_action.lower()

    def test_review_request_shows_link_age_reasks_and_due(self) -> None:
        ticket = TicketFactory(overlay="t3-teatree")
        pr = PullRequestFactory(ticket=ticket, overlay="t3-teatree")
        ticket.extra = {"prs": {pr.url: {"review_permalink": "https://app.slack.com/archives/C1/p1234567890000000"}}}
        ticket.save(update_fields=["extra"])
        ReviewRequestPost.objects.create(
            mr_url=pr.url,
            overlay="t3-teatree",
            slack_channel_id="C1",
            slack_thread_ts="1234567890.000000",
            nag_count=2,
            last_nag_at=timezone.now() - timedelta(days=1),
        )
        overlay = SimpleNamespace(review=SimpleNamespace(repo_owner_for_slug=lambda _slug: RepoOwner.ENGINEERING))
        with patch("teatree.dash.pr_board.get_overlay", return_value=overlay):
            card = next(card for group in build_pr_board().groups for card in group.cards)
        assert card.group == "Waiting on colleagues"
        assert card.review_link.endswith("p1234567890000000")
        assert card.review_reasks == 2
        assert card.review_due != "unknown"
        assert card.review_age != "unknown"

    def test_factory_task_shows_agent_since_and_last_result(self) -> None:
        ticket = TicketFactory()
        PullRequestFactory(ticket=ticket)
        task = TaskFactory(
            ticket=ticket,
            phase="coding",
            status=Task.Status.CLAIMED,
            claimed_by="agent-1",
            claimed_at=timezone.now() - timedelta(hours=2),
        )
        TaskAttemptFactory(task=task, exit_code=0, result={"summary": "tests passed"})

        card = next(card for group in build_pr_board().groups for card in group.cards)
        assert card.group == "Factory working"
        assert card.work_agent == "agent-1"
        assert card.work_since == "2 h"
        assert "tests passed" in card.last_result
