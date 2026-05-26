from django.test import TestCase

from teatree.core.management.commands.e2e import _ticket_frontend_projects
from teatree.core.models import Ticket, Worktree
from teatree.core.runners.worktree_start import compose_project


class TicketFrontendProjectsTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(overlay="demo", issue_url="https://example.test/issues/147")
        cls.test_repo_wt = Worktree.objects.create(ticket=cls.ticket, overlay="demo", repo_path="e2e-tests", branch="b")
        cls.app_repo_wt = Worktree.objects.create(ticket=cls.ticket, overlay="demo", repo_path="webapp", branch="b")

    def test_includes_sibling_app_project_when_resolved_to_test_repo(self) -> None:
        # The bug: discovery only probed the resolved (test-repo) worktree's
        # compose project, never the sibling app worktree hosting the frontend.
        projects = _ticket_frontend_projects(self.test_repo_wt)

        assert projects[0] == "e2e-tests-wt147"
        assert "webapp-wt147" in projects

    def test_resolved_worktree_is_probed_first(self) -> None:
        projects = _ticket_frontend_projects(self.app_repo_wt)

        assert projects[0] == "webapp-wt147"
        assert "e2e-tests-wt147" in projects

    def test_no_duplicate_projects(self) -> None:
        projects = _ticket_frontend_projects(self.test_repo_wt)

        assert len(projects) == len(set(projects))

    def test_ticketless_worktree_yields_only_its_own_project(self) -> None:
        orphan = Worktree.objects.create(
            ticket=Ticket.objects.create(overlay="demo"),
            overlay="demo",
            repo_path="solo",
            branch="b",
        )
        projects = _ticket_frontend_projects(orphan)

        assert projects == [compose_project(orphan)]

    def test_linked_ticket_override_uses_named_tickets_worktrees(self) -> None:
        """``linked_ticket`` reroutes discovery at the named ticket's worktrees.

        Defect 1 of souliane/teatree#1322: when the e2e cache repo's
        auto-registered worktree belongs to a different ticket than the
        backend, the explicit override bypasses the resolved ticket's
        siblings and probes the linked ticket's projects.
        """
        other_ticket = Ticket.objects.create(overlay="demo", issue_url="https://example.test/issues/1322")
        backend_wt = Worktree.objects.create(
            ticket=other_ticket,
            overlay="demo",
            repo_path="backend-repo",
            branch="other",
        )

        projects = _ticket_frontend_projects(self.test_repo_wt, linked_ticket=other_ticket)

        assert compose_project(backend_wt) in projects
        # The resolved-worktree ticket's siblings are bypassed when a link is given.
        assert "webapp-wt147" not in projects
