"""Mechanical handlers for the idle reaper + queue drainer (#2190, #44).

``reap_idle_stack`` re-verifies idle then fires ``stop_services``;
``drain_stack_queue_item`` re-checks the cap then either ``start_services``
(slot freed) or reschedules a Fibonacci backoff. Both re-verify live state
(fail-CLOSED stale-read guard) and never tear down another ticket's stack.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import LocalStackQueueItem, Ticket, Worktree
from teatree.loop.mechanical_local_stack import drain_stack_queue_item, reap_idle_stack


def _worktree(
    *,
    overlay: str = "t3-heavy",
    ticket_number: str,
    state: Worktree.State,
    idle_minutes_ago: int = 60,
) -> Worktree:
    ticket = Ticket.objects.create(
        overlay=overlay,
        issue_url=f"https://example.com/{overlay}/issues/{ticket_number}",
    )
    return Worktree.objects.create(
        overlay=overlay,
        ticket=ticket,
        repo_path="backend",
        branch=f"{ticket_number}-feat",
        state=state,
        db_name=f"wt_{ticket_number}",
        last_used_at=timezone.now() - timedelta(minutes=idle_minutes_ago),
    )


class TestReapIdleStackHandler(TestCase):
    def test_stops_a_still_idle_running_stack(self) -> None:
        wt = _worktree(ticket_number="800", state=Worktree.State.SERVICES_UP)
        with (
            patch(
                "teatree.loop.mechanical_local_stack.reapable_worktrees",
                return_value=[wt],
            ),
        ):
            reap_idle_stack({"worktree_id": wt.pk, "overlay": "t3-heavy"})
        wt.refresh_from_db()
        # The transition demoted it (the on_commit worker runs the docker down).
        assert wt.state == Worktree.State.PROVISIONED
        # REVERSIBLE: the DB is preserved.
        assert wt.db_name == "wt_800"

    def test_does_not_stop_when_re_verify_says_not_idle(self) -> None:
        """Fail-CLOSED stale-read guard: a worktree no longer in the reapable set is kept."""
        wt = _worktree(ticket_number="801", state=Worktree.State.SERVICES_UP)
        with patch("teatree.loop.mechanical_local_stack.reapable_worktrees", return_value=[]):
            reap_idle_stack({"worktree_id": wt.pk, "overlay": "t3-heavy"})
        wt.refresh_from_db()
        assert wt.state == Worktree.State.SERVICES_UP

    def test_missing_worktree_id_is_a_noop(self) -> None:
        # Must not raise.
        reap_idle_stack({"overlay": "t3-heavy"})

    def test_already_gone_worktree_is_a_noop(self) -> None:
        with patch("teatree.loop.mechanical_local_stack.reapable_worktrees", return_value=[]):
            reap_idle_stack({"worktree_id": 999_999, "overlay": "t3-heavy"})

    def test_reapable_but_row_deleted_between_verify_and_lock(self) -> None:
        """The worktree is reapable in the snapshot but its row is gone when locked."""

        class _Ghost:
            pk = 999_998

        with patch("teatree.loop.mechanical_local_stack.reapable_worktrees", return_value=[_Ghost()]):
            # Must not raise — the locked get() raises DoesNotExist and the handler returns.
            reap_idle_stack({"worktree_id": 999_998, "overlay": "t3-heavy"})

    def test_kept_when_stop_not_allowed_after_lock(self) -> None:
        wt = _worktree(ticket_number="805", state=Worktree.State.SERVICES_UP)
        with (
            patch("teatree.loop.mechanical_local_stack.reapable_worktrees", return_value=[wt]),
            patch("teatree.loop.mechanical_local_stack.can_proceed", return_value=False),
        ):
            reap_idle_stack({"worktree_id": wt.pk, "overlay": "t3-heavy"})
        wt.refresh_from_db()
        assert wt.state == Worktree.State.SERVICES_UP


class TestNoStrayContainerAfterReap(TestCase):
    """The reap → stop path compose-downs the WHOLE project (wt595 leak class).

    A reap must leave ZERO container bearing the compose-project label — the
    stop worker calls ``docker compose -p <project> down`` for the whole
    project, never a single service, so a stray ``db-1`` is removed too.
    """

    def test_stop_path_downs_whole_project(self) -> None:
        from teatree.core.worktree.worktree_env import compose_project  # noqa: PLC0415

        wt = _worktree(ticket_number="810", state=Worktree.State.SERVICES_UP)
        expected_project = compose_project(wt)
        with (
            patch("teatree.loop.mechanical_local_stack.reapable_worktrees", return_value=[wt]),
            patch("teatree.core.worktree.worktree_tasks.docker_compose_down") as down,
        ):
            reap_idle_stack({"worktree_id": wt.pk, "overlay": "t3-heavy"})
            # Run the enqueued on_commit worker synchronously to exercise the down.
            from teatree.core.worktree.worktree_tasks import execute_worktree_stop  # noqa: PLC0415

            execute_worktree_stop.call(wt.pk)
        down.assert_called_once()
        assert down.call_args.args[0] == expected_project


class TestDrainStackQueueItemHandler(TestCase):
    def test_acquires_and_starts_when_slot_free(self) -> None:
        wt = _worktree(ticket_number="820", state=Worktree.State.PROVISIONED)
        item = LocalStackQueueItem.objects.create(overlay="t3-heavy", worktree=wt)
        with (
            patch("teatree.loop.mechanical_local_stack.check_local_stack_limit"),  # no raise = slot free
        ):
            drain_stack_queue_item({"queue_item_id": item.pk, "worktree_id": wt.pk, "overlay": "t3-heavy"})
        item.refresh_from_db()
        wt.refresh_from_db()
        assert item.status == LocalStackQueueItem.Status.READY
        assert wt.state == Worktree.State.SERVICES_UP

    def test_reschedules_with_backoff_when_still_full(self) -> None:
        from teatree.core.gates.local_stack_gate import LocalStackLimitExceededError  # noqa: PLC0415

        wt = _worktree(ticket_number="821", state=Worktree.State.PROVISIONED)
        item = LocalStackQueueItem.objects.create(overlay="t3-heavy", worktree=wt)
        with patch(
            "teatree.loop.mechanical_local_stack.check_local_stack_limit",
            side_effect=LocalStackLimitExceededError("full"),
        ):
            drain_stack_queue_item({"queue_item_id": item.pk, "worktree_id": wt.pk, "overlay": "t3-heavy"})
        item.refresh_from_db()
        wt.refresh_from_db()
        # Backoff scheduled; the worktree's FSM was NOT advanced.
        assert item.status == LocalStackQueueItem.Status.RETRYING
        assert item.attempt_count == 1
        assert item.next_attempt_at is not None
        assert wt.state == Worktree.State.PROVISIONED

    def test_never_tears_down_another_tickets_stack(self) -> None:
        """Draining a queued item must only touch its OWN worktree's FSM."""
        from teatree.core.gates.local_stack_gate import LocalStackLimitExceededError  # noqa: PLC0415

        other = _worktree(ticket_number="830", state=Worktree.State.SERVICES_UP)
        wt = _worktree(ticket_number="831", state=Worktree.State.PROVISIONED)
        item = LocalStackQueueItem.objects.create(overlay="t3-heavy", worktree=wt)
        with patch(
            "teatree.loop.mechanical_local_stack.check_local_stack_limit",
            side_effect=LocalStackLimitExceededError("full"),
        ):
            drain_stack_queue_item({"queue_item_id": item.pk, "worktree_id": wt.pk, "overlay": "t3-heavy"})
        other.refresh_from_db()
        # The blocking stack of the OTHER ticket is untouched.
        assert other.state == Worktree.State.SERVICES_UP

    def test_terminal_item_is_a_noop(self) -> None:
        wt = _worktree(ticket_number="840", state=Worktree.State.PROVISIONED)
        item = LocalStackQueueItem.objects.create(
            overlay="t3-heavy",
            worktree=wt,
            status=LocalStackQueueItem.Status.DONE,
        )
        with patch("teatree.loop.mechanical_local_stack.check_local_stack_limit") as check:
            drain_stack_queue_item({"queue_item_id": item.pk, "worktree_id": wt.pk, "overlay": "t3-heavy"})
        check.assert_not_called()

    def test_missing_item_id_is_a_noop(self) -> None:
        drain_stack_queue_item({"overlay": "t3-heavy"})

    def test_already_gone_item_is_a_noop(self) -> None:
        # Must not raise — the locked get() raises DoesNotExist and the handler returns.
        drain_stack_queue_item({"queue_item_id": 999_999, "worktree_id": 1, "overlay": "t3-heavy"})

    def test_kept_when_start_not_allowed(self) -> None:
        """A slot is free but the worktree can't start (e.g. CREATED) → no FSM advance."""
        wt = _worktree(ticket_number="850", state=Worktree.State.CREATED)
        item = LocalStackQueueItem.objects.create(overlay="t3-heavy", worktree=wt)
        with patch("teatree.loop.mechanical_local_stack.check_local_stack_limit"):
            drain_stack_queue_item({"queue_item_id": item.pk, "worktree_id": wt.pk, "overlay": "t3-heavy"})
        wt.refresh_from_db()
        item.refresh_from_db()
        assert wt.state == Worktree.State.CREATED
        assert item.status == LocalStackQueueItem.Status.QUEUED


class TestRunCommandsHelper(TestCase):
    """``_run_commands`` resolves the overlay's commands, ``None`` on any failure."""

    def test_resolves_overlay_commands(self) -> None:
        from unittest.mock import MagicMock  # noqa: PLC0415

        from teatree.loop.mechanical_local_stack import _run_commands  # noqa: PLC0415

        wt = _worktree(ticket_number="859", state=Worktree.State.PROVISIONED)
        overlay = MagicMock()
        overlay.get_run_commands.return_value = ["backend", "frontend"]
        with patch("teatree.core.overlay_loader.get_overlay_for_worktree", return_value=overlay):
            assert _run_commands(wt) == ["backend", "frontend"]

    def test_unresolvable_overlay_returns_none(self) -> None:
        from teatree.loop.mechanical_local_stack import _run_commands  # noqa: PLC0415

        wt = _worktree(ticket_number="860", state=Worktree.State.PROVISIONED)
        with patch(
            "teatree.core.overlay_loader.get_overlay_for_worktree",
            side_effect=RuntimeError("unknown overlay"),
        ):
            assert _run_commands(wt) is None
