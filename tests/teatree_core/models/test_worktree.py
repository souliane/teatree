"""Worktree model tests (souliane/teatree#443 split of test_models.py)."""

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree


class TestWorktree(TestCase):
    def test_lifecycle_transitions_and_stores_urls(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/42", variant="acme")
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/backend", branch="teatree-django")

        worktree.provision()
        worktree.save()
        worktree.start_services(services=["backend", "frontend"])
        worktree.save()
        worktree.verify(urls={"backend": "http://localhost:8001", "frontend": "http://localhost:4201"})
        worktree.save()

        worktree.refresh_from_db()

        assert worktree.state == Worktree.State.READY
        assert worktree.db_name == "wt_42_acme"
        assert worktree.extra["services"] == ["backend", "frontend"]
        assert worktree.extra["urls"] == {
            "backend": "http://localhost:8001",
            "frontend": "http://localhost:4201",
        }
        assert str(worktree) == "/tmp/backend"

    def test_full_lifecycle_with_refresh_and_teardown(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://example.com/issues/100")
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/next", branch="next")

        worktree.provision()
        worktree.save()
        worktree.start_services()
        worktree.save()
        worktree.db_refresh()
        worktree.save()
        worktree.teardown()
        worktree.save()

        worktree.refresh_from_db()

        assert worktree.state == Worktree.State.CREATED
        assert worktree.db_name == ""
        assert worktree.extra == {}

    def test_start_services_allows_restart(self) -> None:
        """Calling start_services when already in SERVICES_UP should work (restart)."""
        ticket = Ticket.objects.create(issue_url="https://example.com/restart", variant="acme")
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/backend", branch="restart")
        worktree.provision()
        worktree.save()
        worktree.start_services(services=["backend"])
        worktree.save()
        assert worktree.state == Worktree.State.SERVICES_UP

        # Restart — should not raise TransitionNotAllowed
        worktree.start_services(services=["backend", "frontend"])
        worktree.save()
        assert worktree.state == Worktree.State.SERVICES_UP
        assert worktree.extra["services"] == ["backend", "frontend"]

    def test_rejects_invalid_transition(self) -> None:
        worktree = Worktree.objects.create(
            ticket=Ticket.objects.create(),
            repo_path="/tmp/backend",
            branch="broken",
        )

        from django_fsm import TransitionNotAllowed  # noqa: PLC0415

        with pytest.raises(TransitionNotAllowed):
            worktree.verify()
