from unittest.mock import patch

from django.test import TestCase

from teatree.core.management.commands._e2e_discovery import resolve_linked_worktree
from teatree.core.management.commands.e2e import _ticket_frontend_projects
from teatree.core.models import Ticket, Worktree
from teatree.core.worktree.worktree_env import compose_project


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

        assert projects[0] == f"e2e-tests-wt{self.ticket.pk}"
        assert f"webapp-wt{self.ticket.pk}" in projects

    def test_resolved_worktree_is_probed_first(self) -> None:
        projects = _ticket_frontend_projects(self.app_repo_wt)

        assert projects[0] == f"webapp-wt{self.ticket.pk}"
        assert f"e2e-tests-wt{self.ticket.pk}" in projects

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
        assert f"webapp-wt{self.ticket.pk}" not in projects


class ResolveLinkedWorktreeTests(TestCase):
    """``--linked-to`` must route at the backend-stack worktree, not the FE.

    Defect 2 of souliane/teatree#1322: ``resolve_linked_worktree`` picked the
    first sibling by pk that had a ``worktree_path``. For a multi-repo ticket
    whose frontend worktree sorts before the backend, that exported the FE's
    ``COMPOSE_PROJECT_NAME`` and ``docker compose exec web`` failed with
    "service web is not running". The resolver must prefer the sibling whose
    overlay returns a non-empty compose file (the backend stack owner).
    """

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(overlay="demo", issue_url="https://example.test/issues/1322")
        # Frontend worktree is created FIRST (lower pk) — it sorts before the
        # backend, reproducing the bug's ordering.
        cls.frontend_wt = Worktree.objects.create(
            ticket=cls.ticket,
            overlay="demo",
            repo_path="frontend-repo",
            branch="b",
            extra={"worktree_path": "/ws/1322/frontend-repo"},
        )
        cls.backend_wt = Worktree.objects.create(
            ticket=cls.ticket,
            overlay="demo",
            repo_path="backend-repo",
            branch="b",
            extra={"worktree_path": "/ws/1322/backend-repo"},
        )

    def _overlay_with_backend(self, backend_repo_path: str):
        def _compose_file(worktree: Worktree) -> str:
            return "/ws/1322/backend-repo/docker-compose.yml" if worktree.repo_path == backend_repo_path else ""

        from unittest.mock import MagicMock  # noqa: PLC0415

        overlay = MagicMock()
        overlay.provisioning.compose_file.side_effect = _compose_file
        return overlay

    def test_prefers_backend_stack_owner_over_first_pk(self) -> None:
        with patch(
            "teatree.core.overlay_loader.get_overlay",
            return_value=self._overlay_with_backend("backend-repo"),
        ):
            resolved = resolve_linked_worktree(self.ticket)

        assert resolved == self.backend_wt

    def test_falls_back_to_first_stored_when_no_backend_signal(self) -> None:
        # A misbehaving / empty overlay (no compose file for any repo) must
        # still route to a worktree — the first stored-path sibling by pk.
        with patch(
            "teatree.core.overlay_loader.get_overlay",
            return_value=self._overlay_with_backend("does-not-exist"),
        ):
            resolved = resolve_linked_worktree(self.ticket)

        assert resolved == self.frontend_wt

    def test_returns_none_when_no_siblings(self) -> None:
        empty = Ticket.objects.create(overlay="demo")
        assert resolve_linked_worktree(empty) is None

    def test_routes_unstored_sibling_when_none_have_paths(self) -> None:
        ticket = Ticket.objects.create(overlay="demo")
        wt = Worktree.objects.create(ticket=ticket, overlay="demo", repo_path="solo", branch="b")
        with patch(
            "teatree.core.overlay_loader.get_overlay",
            return_value=self._overlay_with_backend("solo"),
        ):
            assert resolve_linked_worktree(ticket) == wt

    def test_falls_back_when_overlay_hook_raises(self) -> None:
        # A misbehaving overlay hook must not break routing — the resolver
        # treats a raising ``provisioning.compose_file`` as "not the backend" and falls
        # back to the first stored-path sibling.
        from unittest.mock import MagicMock  # noqa: PLC0415

        overlay = MagicMock()
        overlay.provisioning.compose_file.side_effect = RuntimeError("overlay boom")
        with patch("teatree.core.overlay_loader.get_overlay", return_value=overlay):
            resolved = resolve_linked_worktree(self.ticket)

        assert resolved == self.frontend_wt
