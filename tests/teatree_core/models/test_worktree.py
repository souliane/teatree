"""Worktree model tests (souliane/teatree#443 split of test_models.py)."""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree


class TestWorktreeTransitionSignals(TestCase):
    """The worktree FSM bodies enqueue their workers via post_transition (#2385)."""

    def test_provision_enqueues_worker_after_commit(self) -> None:
        from teatree.core import worktree_tasks as worktree_tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/77", variant="acme")
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/be", branch="b")
        fake = MagicMock()
        with (
            patch.object(worktree_tasks_mod, "execute_worktree_provision", fake),
            self.captureOnCommitCallbacks(execute=True),
        ):
            worktree.provision()
            worktree.save()
        fake.enqueue.assert_called_once_with(worktree.pk)

    def test_start_services_enqueues_worker_after_commit(self) -> None:
        from teatree.core import worktree_tasks as worktree_tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/78")
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/be", branch="b")
        worktree.provision()
        worktree.save()
        fake = MagicMock()
        with (
            patch.object(worktree_tasks_mod, "execute_worktree_start", fake),
            self.captureOnCommitCallbacks(execute=True),
        ):
            worktree.start_services(services=["backend"])
            worktree.save()
        fake.enqueue.assert_called_once_with(worktree.pk)

    def test_teardown_enqueues_worker_with_pre_blank_snapshot(self) -> None:
        """The receiver must enqueue the pre-blank db_name/extra, not the blanked row.

        ``teardown()`` blanks ``db_name`` / ``extra`` IN THE BODY before the
        post_transition receiver fires. The receiver reads the transient
        ``teardown_snapshot`` so the worker still gets the values it needs to
        drop the database and remove the worktree — a regression that read the
        live (blanked) row would enqueue ``snapshot_db_name=""``.
        """
        from teatree.core import worktree_tasks as worktree_tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create(issue_url="https://example.com/issues/79", variant="acme")
        worktree = Worktree.objects.create(ticket=ticket, repo_path="/tmp/be", branch="b")
        worktree.provision()
        worktree.save()
        worktree.refresh_from_db()
        pre_blank_db = worktree.db_name
        assert pre_blank_db == "wt_79_acme"
        worktree.extra = {"worktree_path": "/tmp/wt-79", "services": ["backend"]}
        worktree.save()

        fake = MagicMock()
        with (
            patch.object(worktree_tasks_mod, "execute_worktree_teardown", fake),
            self.captureOnCommitCallbacks(execute=True),
        ):
            worktree.teardown()
            worktree.save()

        fake.enqueue.assert_called_once()
        call_args = fake.enqueue.call_args
        assert call_args.args[0] == worktree.pk
        assert call_args.args[1] == pre_blank_db
        assert call_args.args[2] == {"worktree_path": "/tmp/wt-79", "services": ["backend"]}
        # And the row itself is blanked as before.
        worktree.refresh_from_db()
        assert worktree.db_name == ""
        assert worktree.extra == {}


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
