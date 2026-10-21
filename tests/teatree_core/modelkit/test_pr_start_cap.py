from unittest.mock import patch

import pytest
from django.db.models import QuerySet
from django.db.models.signals import post_save
from django.test import TestCase

from teatree.core.modelkit.pr_start_cap import OPEN_PR_START_CAP, new_pr_start_allowed_q
from teatree.core.models import PullRequest, Session, Task, Ticket
from teatree.core.models.errors import InvalidTransitionError
from teatree.core.signals import _auto_enqueue_task


class TestNewPullRequestStartEligibility(TestCase):
    def setUp(self) -> None:
        post_save.disconnect(_auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")
        self.addCleanup(post_save.connect, _auto_enqueue_task, sender=Task, dispatch_uid="auto_enqueue_task")

    def _task(self, phase: str, ticket: Ticket) -> Task:
        return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)

    def test_cap_holds_source_work_but_not_started_review_or_expedited_work(self) -> None:
        assert OPEN_PR_START_CAP == 3
        tickets = [Ticket.objects.create() for _ in range(OPEN_PR_START_CAP)]
        for index, ticket in enumerate(tickets):
            PullRequest.objects.record_opened(
                ticket=ticket, url=f"https://github.com/example-org/example-repo/pull/{index + 70}"
            )
        ordinary = Ticket.objects.create()
        held = [self._task(phase, ordinary) for phase in ("coding", "testing", "debugging", "e2e", "shipping")]
        started = self._task("coding", tickets[0])
        review = self._task("reviewing", ordinary)
        critic = self._task("critic_reviewing", Ticket.objects.create(role=Ticket.Role.REVIEWER))
        expedited = self._task("shipping", Ticket.objects.create(expedited=True))

        eligible = set(Task.objects.filter(new_pr_start_allowed_q()).values_list("pk", flat=True))

        assert not eligible.intersection(task.pk for task in held)
        assert {started.pk, review.pk, critic.pk, expedited.pk} <= eligible

    def test_closed_pr_does_not_fill_cap(self) -> None:
        for index in range(OPEN_PR_START_CAP):
            ticket = Ticket.objects.create()
            PullRequest.objects.record_opened(
                ticket=ticket, url=f"https://github.com/example-org/example-repo/pull/{index + 80}"
            )
        closed = PullRequest.objects.order_by("pk").first()
        closed.mark_closed()
        closed.save(update_fields=["state"])
        ordinary = self._task("shipping", Ticket.objects.create())

        assert Task.objects.filter(pk=ordinary.pk).filter(new_pr_start_allowed_q()).exists()

    def test_direct_claim_refuses_new_pr_start_at_cap(self) -> None:
        for index in range(OPEN_PR_START_CAP):
            ticket = Ticket.objects.create()
            PullRequest.objects.record_opened(
                ticket=ticket, url=f"https://github.com/example-org/example-repo/pull/{index + 90}"
            )
        held = self._task("shipping", Ticket.objects.create())

        with pytest.raises(InvalidTransitionError, match="open pull request"):
            held.claim(claimed_by="worker")

        held.refresh_from_db()
        assert held.status == Task.Status.PENDING

    def test_record_admission_refuses_new_start_at_cap(self) -> None:
        for index in range(OPEN_PR_START_CAP):
            ticket = Ticket.objects.create()
            PullRequest.objects.record_opened(
                ticket=ticket, url=f"https://github.com/example-org/example-repo/pull/{index + 110}"
            )
        held = self._task("shipping", Ticket.objects.create())

        assert Task.objects.record_admission(held.pk, cheap=False, lane_ceiling=None) is False
        held.refresh_from_db()
        assert held.admitted_at is None

    def test_loop_claim_skips_held_starter_and_claims_started_pr(self) -> None:
        tickets = [Ticket.objects.create() for _ in range(OPEN_PR_START_CAP)]
        for index, ticket in enumerate(tickets):
            PullRequest.objects.record_opened(
                ticket=ticket, url=f"https://github.com/example-org/example-repo/pull/{index + 100}"
            )
        held = self._task("shipping", Ticket.objects.create())
        started = self._task("coding", tickets[0])

        claimed = Task.objects.claim_next_pending(claimed_by="worker")

        assert claimed is not None
        assert claimed.pk == started.pk
        held.refresh_from_db()
        assert held.status == Task.Status.PENDING

    def test_loop_claim_rechecks_cap_after_candidate_selection(self) -> None:
        tickets = [Ticket.objects.create() for _ in range(3)]
        for index, ticket in enumerate(tickets[:2]):
            PullRequest.objects.record_opened(
                ticket=ticket, url=f"https://github.com/example-org/example-repo/pull/{index + 120}"
            )
        held = self._task("shipping", Ticket.objects.create())
        original_first = QuerySet.first
        changed = False

        def open_third_pr(queryset: QuerySet) -> object:
            nonlocal changed
            selected = original_first(queryset)
            if not changed and queryset.model is Task and selected == held.pk:
                changed = True
                PullRequest.objects.record_opened(
                    ticket=tickets[2], url="https://github.com/example-org/example-repo/pull/122"
                )
            return selected

        with patch.object(QuerySet, "first", new=open_third_pr):
            claimed = Task.objects.claim_next_pending(claimed_by="worker")

        assert changed
        assert claimed is None
        held.refresh_from_db()
        assert held.status == Task.Status.PENDING
