"""Worktree model tests (souliane/teatree#443 split of test_models.py)."""

from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.core.models import Ticket, Worktree
from teatree.core.worktree_env import compose_project


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
        assert pre_blank_db == f"wt_{worktree.ticket_id}_acme"
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
        assert worktree.db_name == f"wt_{worktree.ticket_id}_acme"
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


class TestWorktreeDbNameAndPassKeyAreIdentityScoped(TestCase):
    """db_name and the postgres pass key key on the unique Ticket pk (#WT-PR-D finding 10).

    ``ticket_number`` is a DERIVED, non-unique key (trailing issue digits), so
    two tickets on different repos/forges can share one. Keying the database
    name and the secret entry on the immutable Ticket pk instead makes a
    cross-ticket clobber structurally impossible — and stays ticket-scoped (not
    worktree-scoped) so a ticket's sibling repos share one database.
    """

    def test_db_name_keyed_on_ticket_pk_not_ticket_number(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://a.example.com/x/issues/5", variant="acme")
        worktree = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="5-x")
        worktree.provision()
        worktree.save()

        assert worktree.db_name == f"wt_{ticket.pk}_acme"
        assert "wt_5_acme" not in worktree.db_name

    def test_sibling_repos_of_one_ticket_share_db_name(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://a.example.com/x/issues/5", variant="acme")
        backend = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="5-x")
        frontend = Worktree.objects.create(ticket=ticket, repo_path="frontend", branch="5-x")
        backend.provision()
        backend.save()
        frontend.provision()
        frontend.save()

        assert backend.db_name == frontend.db_name

    def test_two_tickets_sharing_trailing_number_get_distinct_db_names(self) -> None:
        ticket_a = Ticket.objects.create(issue_url="https://a.example.com/x/issues/5")
        ticket_b = Ticket.objects.create(issue_url="https://b.example.com/y/issues/5")
        wt_a = Worktree.objects.create(ticket=ticket_a, repo_path="backend", branch="5-a")
        wt_b = Worktree.objects.create(ticket=ticket_b, repo_path="backend", branch="5-b")
        wt_a.provision()
        wt_a.save()
        wt_b.provision()
        wt_b.save()

        assert wt_a.db_name != wt_b.db_name

    def test_pass_key_keyed_on_ticket_pk(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://a.example.com/x/issues/5")
        worktree = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="5-x")

        assert worktree.pass_key == f"teatree/wt/{ticket.pk}/postgres"


class TestAssertDbNameUnclaimed(TestCase):
    """``db_import`` must refuse a db_name another live, foreign-ticket worktree owns."""

    def test_raises_when_another_live_different_ticket_owns_db_name(self) -> None:
        from teatree.core.models.worktree import WorktreeDbNameConflictError  # noqa: PLC0415

        ticket_a = Ticket.objects.create(issue_url="https://a.example.com/x/issues/1")
        ticket_b = Ticket.objects.create(issue_url="https://b.example.com/y/issues/2")
        Worktree.objects.create(
            ticket=ticket_a,
            repo_path="backend",
            branch="1-x",
            db_name="wt_shared",
            state=Worktree.State.PROVISIONED,
        )
        wt_b = Worktree.objects.create(
            ticket=ticket_b,
            repo_path="backend",
            branch="2-x",
            db_name="wt_shared",
            state=Worktree.State.PROVISIONED,
        )

        with pytest.raises(WorktreeDbNameConflictError):
            wt_b.assert_db_name_unclaimed()

    def test_noop_when_db_name_is_unique(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://a.example.com/x/issues/1")
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="1-x",
            db_name="wt_unique",
            state=Worktree.State.PROVISIONED,
        )

        worktree.assert_db_name_unclaimed()  # no raise

    def test_ignores_created_state_row_that_owns_no_db_yet(self) -> None:
        ticket_a = Ticket.objects.create(issue_url="https://a.example.com/x/issues/1")
        ticket_b = Ticket.objects.create(issue_url="https://b.example.com/y/issues/2")
        Worktree.objects.create(
            ticket=ticket_a,
            repo_path="backend",
            branch="1-x",
            db_name="wt_shared",
            state=Worktree.State.CREATED,
        )
        wt_b = Worktree.objects.create(
            ticket=ticket_b,
            repo_path="backend",
            branch="2-x",
            db_name="wt_shared",
            state=Worktree.State.PROVISIONED,
        )

        wt_b.assert_db_name_unclaimed()  # CREATED row owns no DB → no conflict


class TestWorktreeComposeProjectIsIdentityScoped(TestCase):
    """The docker compose project keys on the unique Ticket pk (#2774 follow-up).

    ``ticket_number`` is a DERIVED, non-unique key, so two tickets on different
    repos/forges can share one and collide on a single docker stack
    (``COMPOSE_PROJECT_NAME``). Keying the project name on the immutable Ticket
    pk — frozen on the row at provision time so a running stack is never renamed
    out from under its containers — makes a cross-ticket clobber impossible. A
    ticket's sibling repos each get their OWN stack (distinct ``repo_path``),
    unlike the ticket-shared db_name.
    """

    def test_compose_project_keyed_on_ticket_pk_not_ticket_number(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://a.example.com/x/issues/5")
        worktree = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="5-x")
        worktree.provision()
        worktree.save()

        assert worktree.compose_project == f"backend-wt{ticket.pk}"
        assert worktree.compose_project != "backend-wt5"

    def test_two_tickets_sharing_trailing_number_get_distinct_compose_projects(self) -> None:
        ticket_a = Ticket.objects.create(issue_url="https://a.example.com/x/issues/5")
        ticket_b = Ticket.objects.create(issue_url="https://b.example.com/y/issues/5")
        wt_a = Worktree.objects.create(ticket=ticket_a, repo_path="backend", branch="5-a")
        wt_b = Worktree.objects.create(ticket=ticket_b, repo_path="backend", branch="5-b")
        wt_a.provision()
        wt_a.save()
        wt_b.provision()
        wt_b.save()

        assert compose_project(wt_a) != compose_project(wt_b)

    def test_sibling_repos_of_one_ticket_get_distinct_compose_projects(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://a.example.com/x/issues/5")
        backend = Worktree.objects.create(ticket=ticket, repo_path="backend", branch="5-x")
        frontend = Worktree.objects.create(ticket=ticket, repo_path="frontend", branch="5-x")
        backend.provision()
        backend.save()
        frontend.provision()
        frontend.save()

        assert backend.compose_project != frontend.compose_project

    def test_provision_does_not_rename_an_already_stored_project(self) -> None:
        # Sticky: a worktree whose compose project was set under a previous scheme
        # (e.g. backfilled to its running stack's name) keeps that name across
        # re-provision — renaming it would orphan its live containers.
        ticket = Ticket.objects.create(issue_url="https://a.example.com/x/issues/5")
        worktree = Worktree.objects.create(
            ticket=ticket, repo_path="backend", branch="5-x", compose_project="backend-wt5"
        )
        worktree.provision()
        worktree.save()

        assert worktree.compose_project == "backend-wt5"


class TestAssertComposeProjectUnclaimed(TestCase):
    """``worktree start`` must refuse a compose project a foreign live worktree owns."""

    def test_raises_when_another_live_different_ticket_owns_compose_project(self) -> None:
        from teatree.core.models.worktree import WorktreeComposeProjectConflictError  # noqa: PLC0415

        ticket_a = Ticket.objects.create(issue_url="https://a.example.com/x/issues/1")
        ticket_b = Ticket.objects.create(issue_url="https://b.example.com/y/issues/2")
        Worktree.objects.create(
            ticket=ticket_a,
            repo_path="backend",
            branch="1-x",
            compose_project="backend-wt-shared",
            state=Worktree.State.SERVICES_UP,
        )
        wt_b = Worktree.objects.create(
            ticket=ticket_b,
            repo_path="backend",
            branch="2-x",
            compose_project="backend-wt-shared",
            state=Worktree.State.PROVISIONED,
        )

        with pytest.raises(WorktreeComposeProjectConflictError):
            wt_b.assert_compose_project_unclaimed()

    def test_noop_when_compose_project_is_unique(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://a.example.com/x/issues/1")
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="1-x",
            compose_project="backend-wt-unique",
            state=Worktree.State.PROVISIONED,
        )

        worktree.assert_compose_project_unclaimed()  # no raise

    def test_noop_when_compose_project_unset(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://a.example.com/x/issues/1")
        worktree = Worktree.objects.create(
            ticket=ticket,
            repo_path="backend",
            branch="1-x",
            state=Worktree.State.CREATED,
        )

        worktree.assert_compose_project_unclaimed()  # empty project → nothing to claim

    def test_ignores_created_state_row_that_owns_no_stack_yet(self) -> None:
        ticket_a = Ticket.objects.create(issue_url="https://a.example.com/x/issues/1")
        ticket_b = Ticket.objects.create(issue_url="https://b.example.com/y/issues/2")
        Worktree.objects.create(
            ticket=ticket_a,
            repo_path="backend",
            branch="1-x",
            compose_project="backend-wt-shared",
            state=Worktree.State.CREATED,
        )
        wt_b = Worktree.objects.create(
            ticket=ticket_b,
            repo_path="backend",
            branch="2-x",
            compose_project="backend-wt-shared",
            state=Worktree.State.PROVISIONED,
        )

        wt_b.assert_compose_project_unclaimed()  # CREATED row owns no stack → no conflict
