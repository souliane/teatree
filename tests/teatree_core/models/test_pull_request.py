"""Admin registration and PullRequest model tests.

souliane/teatree#443 split of test_models.py.
"""

from django.contrib import admin
from django.test import TestCase

from teatree.core import admin as core_admin
from teatree.core.models import PullRequest, Session, Task, TaskAttempt, Ticket, Worktree


class TestAdmin(TestCase):
    def test_registers_all_core_models(self) -> None:
        registry = admin.site._registry

        assert Ticket in registry
        assert Worktree in registry
        assert Session in registry
        assert Task in registry
        assert TaskAttempt in registry
        assert core_admin is not None


class TestPullRequestModel(TestCase):
    def test_str_representation(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        pr = PullRequest.objects.create(
            ticket=ticket,
            url="https://example.com/repo/-/merge_requests/1",
            repo="my-repo",
            iid="42",
        )
        assert str(pr) == "my-repo #42"

    def test_request_review_transition(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        pr = PullRequest.objects.create(
            ticket=ticket,
            url="https://example.com/repo/-/merge_requests/2",
            repo="my-repo",
            iid="43",
        )
        pr.request_review(slack_url="https://slack.com/msg/123")
        pr.save()
        pr.refresh_from_db()
        assert pr.state == PullRequest.State.REVIEW_REQUESTED
        assert pr.slack_url == "https://slack.com/msg/123"
        assert pr.review_requested_at is not None

    def test_approve_transition(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        pr = PullRequest.objects.create(
            ticket=ticket,
            url="https://example.com/repo/-/merge_requests/3",
            repo="my-repo",
            iid="44",
            state=PullRequest.State.REVIEW_REQUESTED,
        )
        pr.approve()
        pr.save()
        assert pr.state == PullRequest.State.APPROVED

    def test_mark_merged_transition(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        pr = PullRequest.objects.create(
            ticket=ticket,
            url="https://example.com/repo/-/merge_requests/4",
            repo="my-repo",
            iid="45",
        )
        pr.mark_merged()
        pr.save()
        assert pr.state == PullRequest.State.MERGED


_URL = "https://github.com/acme/widget/pull/42"


def _row(ticket: Ticket, *, slug: str = "acme/widget", pr_id: int = 42, url: str = _URL) -> PullRequest:
    return PullRequest.objects.create(ticket=ticket, overlay=ticket.overlay, url=url, repo=slug, iid=str(pr_id))


class TestOwningTicket(TestCase):
    def test_resolves_through_the_pull_request_foreign_key(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        _row(ticket)

        assert PullRequest.objects.owning_ticket(slug="acme/widget", pr_id=42) == ticket

    def test_falls_back_to_the_ticket_extra_prs_map(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", extra={"prs": {_URL: {}}})

        assert PullRequest.objects.owning_ticket(slug="acme/widget", pr_id=42, pr_url=_URL) == ticket

    def test_unlinked_pull_request_resolves_to_nothing(self) -> None:
        Ticket.objects.create(overlay="t3-teatree")

        assert PullRequest.objects.owning_ticket(slug="acme/widget", pr_id=42, pr_url=_URL) is None

    def test_a_different_repo_with_the_same_number_is_not_the_owner(self) -> None:
        _row(Ticket.objects.create(overlay="t3-teatree"))

        assert PullRequest.objects.owning_ticket(slug="acme/gadget", pr_id=42) is None

    def test_the_extra_prs_fallback_runs_without_being_handed_the_pr_url(self) -> None:
        """The backfill knows only ``(slug, pr_id)`` — a CLEAR carries no PR url."""
        ticket = Ticket.objects.create(overlay="t3-teatree", extra={"prs": {_URL: {}}})

        assert PullRequest.objects.owning_ticket(slug="acme/widget", pr_id=42) == ticket

    def test_the_extra_prs_fallback_ignores_a_different_pr_on_the_same_repo(self) -> None:
        Ticket.objects.create(overlay="t3-teatree", extra={"prs": {_URL: {}}})

        assert PullRequest.objects.owning_ticket(slug="acme/widget", pr_id=43) is None


class TestForgeSlugsAreCaseInsensitive(TestCase):
    """A forge slug is case-insensitive, so a mis-cased row must still be found.

    ``execution``'s sibling supersede and ``merge_quality_gate``'s ticket
    resolution both already match ``__iexact`` for exactly this reason. A
    case-sensitive PR lookup silently marks 0 rows and resolves no ticket, so
    the keystone advances nothing and the board starves behind a real merge.
    """

    def test_owning_ticket_resolves_a_differently_cased_slug(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        _row(ticket, slug="Acme/Widget")

        assert PullRequest.objects.owning_ticket(slug="acme/widget", pr_id=42) == ticket

    def test_record_forge_merge_marks_a_differently_cased_slug(self) -> None:
        row = _row(Ticket.objects.create(overlay="t3-teatree"), slug="Acme/Widget")

        assert PullRequest.objects.record_forge_merge(slug="acme/widget", pr_id=42) == 1

        row.refresh_from_db()
        assert row.state == PullRequest.State.MERGED

    def test_the_extra_prs_fallback_matches_a_differently_cased_slug(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            extra={"prs": {"https://github.com/Acme/Widget/pull/42": {}}},
        )

        assert PullRequest.objects.owning_ticket(slug="acme/widget", pr_id=42) == ticket


class TestRecordForgeMerge(TestCase):
    def test_open_row_transitions_to_merged(self) -> None:
        row = _row(Ticket.objects.create(overlay="t3-teatree"))

        assert PullRequest.objects.record_forge_merge(slug="acme/widget", pr_id=42) == 1

        row.refresh_from_db()
        assert row.state == PullRequest.State.MERGED

    def test_already_merged_row_is_a_no_op(self) -> None:
        row = _row(Ticket.objects.create(overlay="t3-teatree"))
        row.mark_merged()
        row.save()

        assert PullRequest.objects.record_forge_merge(slug="acme/widget", pr_id=42) == 0

    def test_review_requested_and_approved_rows_both_advance(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        requested = _row(ticket, pr_id=7, url="https://github.com/acme/widget/pull/7")
        requested.request_review()
        requested.save()
        approved = _row(ticket, pr_id=8, url="https://github.com/acme/widget/pull/8")
        approved.request_review()
        approved.approve()
        approved.save()

        assert PullRequest.objects.record_forge_merge(slug="acme/widget", pr_id=7) == 1
        assert PullRequest.objects.record_forge_merge(slug="acme/widget", pr_id=8) == 1

        requested.refresh_from_db()
        approved.refresh_from_db()
        assert requested.state == PullRequest.State.MERGED
        assert approved.state == PullRequest.State.MERGED

    def test_no_row_for_the_pr_is_a_no_op(self) -> None:
        assert PullRequest.objects.record_forge_merge(slug="acme/widget", pr_id=99) == 0


class TestOwningTicketReadsTheDeliveryPipelinesOwnIndex(TestCase):
    """The pipeline's record of the PR it opened must resolve the owning ticket (#3840).

    ``ShipExecutor`` records every PR it opens under ``extra["pr_urls"]`` and the
    per-branch ``extra["pr_url_by_branch"]`` index. Resolving only ``extra["prs"]``
    — the key the GitLab PR sync writes — left the keystone's post hook with no
    ticket to advance for a PR the pipeline itself opened: the merge landed, the
    CLEAR was consumed, the audit row was written, and the FSM never moved.
    """

    def test_resolves_through_the_per_branch_index(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", extra={"pr_url_by_branch": {"feat/x": _URL}})

        assert PullRequest.objects.owning_ticket(slug="acme/widget", pr_id=42) == ticket

    def test_resolves_through_the_recorded_url_list(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", extra={"pr_urls": [_URL]})

        assert PullRequest.objects.owning_ticket(slug="acme/widget", pr_id=42) == ticket

    def test_a_different_pr_in_the_index_is_not_the_owner(self) -> None:
        Ticket.objects.create(overlay="t3-teatree", extra={"pr_url_by_branch": {"feat/x": _URL}})

        assert PullRequest.objects.owning_ticket(slug="acme/widget", pr_id=43) is None

    def test_the_index_matches_a_differently_cased_slug(self) -> None:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            extra={"pr_urls": ["https://github.com/Acme/Widget/pull/42"]},
        )

        assert PullRequest.objects.owning_ticket(slug="acme/widget", pr_id=42) == ticket


class TestRecordOpened(TestCase):
    """The arbiter row is written when the pipeline opens the PR, not a tick later.

    ``PullRequest`` is the PR-facts arbiter every merge-time consumer reads, yet the
    only writer was the tick-time open-PR reconciler — so a PR that opened and merged
    between two ticks never got a row at all, and the keystone had nothing to resolve.
    """

    def test_persists_the_row_for_the_opened_pr(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")

        row = PullRequest.objects.record_opened(ticket=ticket, url=_URL)

        assert row is not None
        assert row.ticket == ticket
        assert row.repo == "acme/widget"
        assert row.iid == "42"
        assert row.overlay == "t3-teatree"
        assert row.create_verification == PullRequest.CreateVerification.CONFIRMED
        assert row.create_verified_at is not None

    def test_a_retry_reuses_the_existing_row(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")

        first = PullRequest.objects.record_opened(ticket=ticket, url=_URL)
        second = PullRequest.objects.record_opened(ticket=ticket, url=_URL)

        assert first is not None
        assert second is not None
        assert first.pk == second.pk
        assert PullRequest.objects.filter(url=_URL).count() == 1

    def test_a_url_that_names_no_pull_request_writes_nothing(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")

        assert PullRequest.objects.record_opened(ticket=ticket, url="https://example.com/pr/b-new") is None
        assert PullRequest.objects.count() == 0
