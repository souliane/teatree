"""``Worktree.stop_services`` reversible transition + ``last_used_at`` stamp (#2190).

The idle-stack reaper demotes an idle ``services_up``/``ready`` worktree to
``provisioned`` by stopping its containers — REVERSIBLE: the DB and worktree
are preserved, only docker is brought down. ``last_used_at`` is the activity
recency signal the reaper consults; it is stamped on start/verify/db_refresh.
"""

from django.test import TestCase
from django.utils import timezone
from django_fsm import TransitionNotAllowed, can_proceed

from teatree.core.models import Ticket, Worktree


def _worktree(*, state: Worktree.State, ticket_number: str = "42") -> Worktree:
    ticket = Ticket.objects.create(issue_url=f"https://example.com/issues/{ticket_number}")
    return Worktree.objects.create(
        ticket=ticket,
        repo_path="backend",
        branch=f"{ticket_number}-feat",
        state=state,
        db_name="wt_42",
        extra={"worktree_path": "/tmp/wt", "ports": {"backend": 8001}},
    )


class TestLastUsedAtStamping(TestCase):
    """``last_used_at`` records activity recency for the idle reaper."""

    def test_default_is_null(self) -> None:
        wt = _worktree(state=Worktree.State.CREATED)
        assert wt.last_used_at is None

    def test_start_services_stamps_last_used_at(self) -> None:
        wt = _worktree(state=Worktree.State.PROVISIONED)
        before = timezone.now()
        wt.start_services(services=["backend"])
        wt.save()
        wt.refresh_from_db()
        assert wt.last_used_at is not None
        assert wt.last_used_at >= before

    def test_verify_stamps_last_used_at(self) -> None:
        wt = _worktree(state=Worktree.State.SERVICES_UP)
        before = timezone.now()
        wt.verify()
        wt.save()
        wt.refresh_from_db()
        assert wt.last_used_at is not None
        assert wt.last_used_at >= before

    def test_db_refresh_stamps_last_used_at(self) -> None:
        wt = _worktree(state=Worktree.State.READY)
        before = timezone.now()
        wt.db_refresh()
        wt.save()
        wt.refresh_from_db()
        assert wt.last_used_at is not None
        assert wt.last_used_at >= before


class TestStopServicesTransition(TestCase):
    """``stop_services`` demotes a running worktree to ``provisioned`` (reversible)."""

    def test_services_up_can_stop(self) -> None:
        wt = _worktree(state=Worktree.State.SERVICES_UP)
        assert can_proceed(wt.stop_services)

    def test_ready_can_stop(self) -> None:
        wt = _worktree(state=Worktree.State.READY)
        assert can_proceed(wt.stop_services)

    def test_provisioned_cannot_stop(self) -> None:
        wt = _worktree(state=Worktree.State.PROVISIONED)
        assert not can_proceed(wt.stop_services)

    def test_created_cannot_stop(self) -> None:
        wt = _worktree(state=Worktree.State.CREATED)
        assert not can_proceed(wt.stop_services)

    def test_stop_demotes_to_provisioned(self) -> None:
        wt = _worktree(state=Worktree.State.READY)
        wt.stop_services()
        wt.save()
        wt.refresh_from_db()
        assert wt.state == Worktree.State.PROVISIONED

    def test_stop_preserves_db_name_and_worktree_path(self) -> None:
        """REVERSIBLE: stopping must NOT clear db_name or extra (unlike teardown)."""
        wt = _worktree(state=Worktree.State.SERVICES_UP)
        wt.stop_services()
        wt.save()
        wt.refresh_from_db()
        assert wt.db_name == "wt_42"
        assert wt.worktree_path == "/tmp/wt"

    def test_stop_then_restart_is_a_round_trip(self) -> None:
        """After stop → provisioned, start_services brings it back with no data loss."""
        wt = _worktree(state=Worktree.State.READY)
        wt.stop_services()
        wt.save()
        wt.refresh_from_db()
        assert wt.state == Worktree.State.PROVISIONED
        # DB preserved, so a restart is a fast resume, not a re-import.
        assert wt.db_name == "wt_42"
        wt.start_services()
        wt.save()
        wt.refresh_from_db()
        assert wt.state == Worktree.State.SERVICES_UP
        assert wt.db_name == "wt_42"

    def test_stop_from_provisioned_raises_transition_not_allowed(self) -> None:
        wt = _worktree(state=Worktree.State.PROVISIONED)
        try:
            wt.stop_services()
        except TransitionNotAllowed:
            pass
        else:  # pragma: no cover - the assertion below makes the failure explicit
            msg = "stop_services should not be allowed from PROVISIONED"
            raise AssertionError(msg)
