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
