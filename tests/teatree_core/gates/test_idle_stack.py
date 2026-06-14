"""Idle-stack detection — the reapable-worktree predicate (#2190).

A locally-running worktree (``services_up``/``ready``) is REAPABLE when there
is no live Session and no active/claimed Task on its ticket, AND
``last_used_at`` is older than the idle threshold, AND it is not the
currently-active worktree (the CWD), AND its docker stack is real OR a db-only
partial stack (the wt595 leak class).

Fail-safe: any uncertainty ⇒ KEEP (never reaped). The anti-vacuous core of
the suite — reverting the active-session / active-task / CWD guard must turn an
``active-stack-NOT-reaped`` test RED.
"""

from datetime import timedelta
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.gates import idle_stack as idle_mod
from teatree.core.gates.idle_stack import classify_running_worktrees, reapable_worktrees
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.core.models.external_delivery import mark_external_delivery


def _running_worktree(
    *,
    overlay: str = "t3-heavy",
    ticket_number: str = "100",
    state: Worktree.State = Worktree.State.SERVICES_UP,
    idle_minutes_ago: int = 60,
    worktree_path: str = "",
) -> Worktree:
    ticket = Ticket.objects.create(
        overlay=overlay,
        issue_url=f"https://example.com/{overlay}/issues/{ticket_number}",
    )
    extra: dict[str, str] = {}
    if worktree_path:
        extra["worktree_path"] = worktree_path
    return Worktree.objects.create(
        overlay=overlay,
        ticket=ticket,
        repo_path="backend",
        branch=f"{ticket_number}-feat",
        state=state,
        db_name=f"wt_{ticket_number}",
        last_used_at=timezone.now() - timedelta(minutes=idle_minutes_ago),
        extra=extra,
    )


class _StackLiveBase(TestCase):
    """Default every stack to a real (running) docker stack.

    Keeps the partial-stack reconcile from interfering with the idle-predicate
    behaviour tests.
    """

    def setUp(self) -> None:
        super().setUp()
        running = patch.object(idle_mod, "_running_container_count", return_value=1)
        running.start()
        self.addCleanup(running.stop)


class TestReapableHappyPath(_StackLiveBase):
    def test_idle_running_worktree_is_reapable(self) -> None:
        wt = _running_worktree()
        reapable = list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))
        assert wt in reapable

    def test_ready_state_is_reapable_too(self) -> None:
        wt = _running_worktree(state=Worktree.State.READY)
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))


class TestActiveStackNotReaped(_StackLiveBase):
    """The anti-vacuous core: an ACTIVE stack must never be reaped.

    Revert the corresponding guard in ``idle_stack.py`` and each of these
    goes RED (the active stack would be wrongly reaped).
    """

    def test_live_session_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="200")
        Session.objects.create(overlay="t3-heavy", ticket=wt.ticket, ended_at=None)
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_ended_session_does_not_keep_stack(self) -> None:
        wt = _running_worktree(ticket_number="201")
        Session.objects.create(overlay="t3-heavy", ticket=wt.ticket, ended_at=timezone.now())
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_pending_task_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="202")
        session = Session.objects.create(overlay="t3-heavy", ticket=wt.ticket, ended_at=timezone.now())
        Task.objects.create(ticket=wt.ticket, session=session, status=Task.Status.PENDING)
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_claimed_task_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="203")
        session = Session.objects.create(overlay="t3-heavy", ticket=wt.ticket, ended_at=timezone.now())
        Task.objects.create(ticket=wt.ticket, session=session, status=Task.Status.CLAIMED)
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_completed_task_does_not_keep_stack(self) -> None:
        wt = _running_worktree(ticket_number="204")
        session = Session.objects.create(overlay="t3-heavy", ticket=wt.ticket, ended_at=timezone.now())
        Task.objects.create(ticket=wt.ticket, session=session, status=Task.Status.COMPLETED)
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_recently_used_stack_is_kept(self) -> None:
        wt = _running_worktree(ticket_number="205", idle_minutes_ago=5)
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_null_last_used_at_is_kept_fail_safe(self) -> None:
        """A worktree with no recorded activity cannot be confirmed idle ⇒ KEEP."""
        wt = _running_worktree(ticket_number="206")
        wt.last_used_at = None
        wt.save(update_fields=["last_used_at"])
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_currently_active_worktree_is_kept(self) -> None:
        """The CWD's own worktree is never reaped even when otherwise idle."""
        wt = _running_worktree(ticket_number="207", worktree_path="/ws/207-feat/backend")
        with patch.object(idle_mod, "_active_worktree_path", return_value=Path("/ws/207-feat/backend")):
            assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_provisioned_worktree_is_not_a_candidate(self) -> None:
        """PROVISIONED holds no docker stack — nothing to reap."""
        wt = _running_worktree(ticket_number="208", state=Worktree.State.PROVISIONED)
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))


class TestActiveDeliveryNotReaped(_StackLiveBase):
    """#2227: a stack under active delivery / fresh E2E evidence / a pin is KEPT.

    The anti-vacuous core for #2227: revert any one guard in
    ``idle_stack.preserve_reason`` and the matching test goes RED (the live
    target of in-flight work would be wrongly reaped, forcing a re-provision).
    A genuinely idle stack carrying NONE of the three is still reaped.
    """

    def test_live_external_delivery_lease_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="220")
        mark_external_delivery(wt.ticket)
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_expired_external_delivery_lease_does_not_keep_stack(self) -> None:
        wt = _running_worktree(ticket_number="221")
        mark_external_delivery(wt.ticket, lease_seconds=-1)
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_recent_e2e_run_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="222")
        wt.last_e2e_run = timezone.now() - timedelta(minutes=5)
        wt.save(update_fields=["last_e2e_run"])
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30, e2e_recent_minutes=60))

    def test_stale_e2e_run_does_not_keep_stack(self) -> None:
        wt = _running_worktree(ticket_number="223")
        wt.last_e2e_run = timezone.now() - timedelta(minutes=120)
        wt.save(update_fields=["last_e2e_run"])
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30, e2e_recent_minutes=60))

    def test_null_e2e_run_does_not_keep_stack(self) -> None:
        wt = _running_worktree(ticket_number="224")
        assert wt.last_e2e_run is None
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30, e2e_recent_minutes=60))

    def test_explicit_pin_keeps_stack(self) -> None:
        wt = _running_worktree(ticket_number="225")
        wt.extra = {**wt.extra, "reaper_pinned": True}
        wt.save(update_fields=["extra"])
        assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_genuinely_idle_with_none_is_reaped(self) -> None:
        wt = _running_worktree(ticket_number="226")
        assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30, e2e_recent_minutes=60))

    def test_preserve_reason_names_the_lease(self) -> None:
        wt = _running_worktree(ticket_number="227")
        mark_external_delivery(wt.ticket)
        classified = dict(classify_running_worktrees(overlay="t3-heavy", idle_minutes=30))
        assert classified[wt] is not None
        assert "external-delivery lease" in classified[wt]

    def test_preserve_reason_names_the_e2e_run(self) -> None:
        wt = _running_worktree(ticket_number="228")
        wt.last_e2e_run = timezone.now() - timedelta(minutes=5)
        wt.save(update_fields=["last_e2e_run"])
        classified = dict(classify_running_worktrees(overlay="t3-heavy", idle_minutes=30, e2e_recent_minutes=60))
        assert classified[wt] is not None
        assert "E2E" in classified[wt]

    def test_preserve_reason_names_the_pin(self) -> None:
        wt = _running_worktree(ticket_number="229")
        wt.extra = {**wt.extra, "reaper_pinned": True}
        wt.save(update_fields=["extra"])
        classified = dict(classify_running_worktrees(overlay="t3-heavy", idle_minutes=30))
        assert classified[wt] is not None
        assert "pinned" in classified[wt]

    def test_reapable_idle_classifies_with_no_reason(self) -> None:
        wt = _running_worktree(ticket_number="230")
        classified = dict(classify_running_worktrees(overlay="t3-heavy", idle_minutes=30, e2e_recent_minutes=60))
        assert classified[wt] is None


class TestEffectiveE2eWindowFromConfig(_StackLiveBase):
    """The E2E window defaults to ``idle_stack_e2e_recent_minutes`` when not passed."""

    def test_default_window_keeps_a_recent_e2e_run(self) -> None:
        wt = _running_worktree(ticket_number="240")
        wt.last_e2e_run = timezone.now() - timedelta(minutes=5)
        wt.save(update_fields=["last_e2e_run"])
        with patch.object(idle_mod, "get_effective_settings") as settings:
            settings.return_value.idle_stack_e2e_recent_minutes = 60
            assert wt not in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_zero_window_disables_the_e2e_guard(self) -> None:
        wt = _running_worktree(ticket_number="241")
        wt.last_e2e_run = timezone.now() - timedelta(minutes=5)
        wt.save(update_fields=["last_e2e_run"])
        with patch.object(idle_mod, "get_effective_settings") as settings:
            settings.return_value.idle_stack_e2e_recent_minutes = 0
            assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))


class TestCrossOverlayScope(_StackLiveBase):
    def test_other_overlays_worktrees_are_not_returned(self) -> None:
        _running_worktree(overlay="t3-other", ticket_number="300")
        mine = _running_worktree(overlay="t3-heavy", ticket_number="301")
        reapable = list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))
        assert mine in reapable
        assert all(w.overlay == "t3-heavy" for w in reapable)


class TestPartialStackReconcile(TestCase):
    """A db-only partial stack (app tier down, db lingering) is reapable.

    The wt595 leak class: ``docker ps`` (running) shows the app tier down but a
    stray ``db-1`` survives. The reaper must treat that as reapable (stop the
    WHOLE project), NOT as a healthy stack to keep.
    """

    def test_db_only_partial_stack_is_reapable(self) -> None:
        wt = _running_worktree(ticket_number="400")
        # One stray container (the leaked db) still running.
        with patch.object(idle_mod, "_running_container_count", return_value=1):
            assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))

    def test_zero_container_stack_is_still_reapable(self) -> None:
        """A fully-gone stack is also reapable (idempotent stop is a no-op)."""
        wt = _running_worktree(ticket_number="401")
        with patch.object(idle_mod, "_running_container_count", return_value=0):
            assert wt in list(reapable_worktrees(overlay="t3-heavy", idle_minutes=30))


class TestRunningContainerCountHelper(TestCase):
    """``_running_container_count`` maps docker output → count (advisory only)."""

    @staticmethod
    def _result(returncode: int, stdout: str) -> "CompletedProcess[str]":
        return CompletedProcess(["docker", "ps"], returncode, stdout, "")

    def test_blank_project_is_minus_one(self) -> None:
        assert idle_mod._running_container_count("") == -1

    def test_docker_failure_is_minus_one(self) -> None:
        with patch.object(idle_mod, "run_allowed_to_fail", return_value=self._result(1, "")):
            assert idle_mod._running_container_count("p") == -1

    def test_counts_nonblank_names(self) -> None:
        with patch.object(idle_mod, "run_allowed_to_fail", return_value=self._result(0, "c1\n\nc2\n")):
            assert idle_mod._running_container_count("p") == 2


class TestActiveWorktreePathHelper(TestCase):
    """``_active_worktree_path`` returns the resolved CWD, ``None`` on OSError."""

    def test_returns_none_on_oserror(self) -> None:
        with patch.object(idle_mod.Path, "cwd", side_effect=OSError):
            assert idle_mod._active_worktree_path() is None


class TestIsCurrentlyActiveHelper(TestCase):
    """``_is_currently_active`` matches a worktree's own dir or a child of it."""

    def test_none_active_path_is_not_active(self) -> None:
        wt = _running_worktree(ticket_number="900", worktree_path="/ws/900/backend")
        assert idle_mod._is_currently_active(wt, None) is False

    def test_blank_worktree_path_is_not_active(self) -> None:
        wt = _running_worktree(ticket_number="901")  # no worktree_path
        assert idle_mod._is_currently_active(wt, Path("/ws/901/backend")) is False

    def test_child_of_worktree_is_active(self) -> None:
        wt = _running_worktree(ticket_number="902", worktree_path="/ws/902/backend")
        assert idle_mod._is_currently_active(wt, Path("/ws/902/backend/src/app")) is True


class TestPreserveReasonFailSafeGuards(TestCase):
    """``preserve_reason`` directly — the defensive fail-safe guards still hold.

    A non-``None`` reason means KEEP; ``None`` means reapable.
    """

    def _cutoff(self) -> object:
        return timezone.now() - timedelta(minutes=30)

    def _e2e_cutoff(self) -> object:
        return timezone.now() - timedelta(minutes=60)

    def test_non_running_state_cannot_proceed_is_kept(self) -> None:
        """A PROVISIONED row can't ``stop_services`` → kept (the can_proceed guard)."""
        wt = _running_worktree(ticket_number="910", state=Worktree.State.PROVISIONED)
        reason = idle_mod.preserve_reason(wt, cutoff=self._cutoff(), e2e_cutoff=self._e2e_cutoff(), active_path=None)
        assert reason is not None

    def test_null_last_used_at_is_kept(self) -> None:
        wt = _running_worktree(ticket_number="911")
        wt.last_used_at = None
        with patch.object(idle_mod, "_running_container_count", return_value=1):
            reason = idle_mod.preserve_reason(
                wt, cutoff=self._cutoff(), e2e_cutoff=self._e2e_cutoff(), active_path=None
            )
        assert reason is not None
