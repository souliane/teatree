"""Tests for :mod:`teatree.loop.self_update_reinstall` — the deferred drain (#1760).

The drain runs as the first step of each per-tick subprocess. It is a no-op
when nothing is pending, DEFERS while a loop unit is in flight (a live CLAIMED
lease), and otherwise re-anchors the running editable install. The reinstall +
self-DB migrate themselves are :mod:`teatree.self_update` primitives (tested
there); here we assert only the drain's gating + bookkeeping.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

import teatree.self_update as self_update_mod
from teatree.core.models.pending_reinstall import PendingReinstall
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loop.self_update_reinstall import DrainOutcome, drain_pending_reinstall
from teatree.self_update import ReinstallResult

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _ok_reinstall() -> ReinstallResult:
    return ReinstallResult(ok=True, reinstalled=True)


class _Patches:
    """Patch the reinstall + migrate primitives the drain shells out to."""

    @staticmethod
    def green(*, migrate_unmigrated: bool = False):
        return (
            patch.object(self_update_mod, "reinstall_running_editable", _ok_reinstall),
            patch.object(self_update_mod, "ensure_self_db_migrated", lambda: migrate_unmigrated),
        )


class DrainNoopAndDeferTests(TestCase):
    def test_noop_when_nothing_pending(self) -> None:
        result = drain_pending_reinstall()

        assert result.outcome is DrainOutcome.NOOP

    def test_done_states_are_not_treated_as_pending(self) -> None:
        PendingReinstall.objects.create(
            repo_label="teatree",
            target_sha="abc",
            state=PendingReinstall.State.DONE,
        )

        result = drain_pending_reinstall()

        assert result.outcome is DrainOutcome.NOOP

    def test_defers_while_a_loop_unit_is_in_flight(self) -> None:
        PendingReinstall.objects.upsert_pending(repo_label="teatree", target_sha="abc")
        self._claimed_task(lease_seconds=300)
        called: list[bool] = []

        with patch.object(self_update_mod, "reinstall_running_editable", lambda: called.append(True)):
            result = drain_pending_reinstall()

        assert result.outcome is DrainOutcome.DEFERRED
        assert result.repo_label == "teatree"
        assert called == [], "the reinstall must NOT run while a unit is in flight"
        # The row stays pending so the next clean tick can drain it.
        assert PendingReinstall.objects.get(repo_label="teatree").state == PendingReinstall.State.PENDING

    def test_expired_lease_is_not_in_flight(self) -> None:
        PendingReinstall.objects.upsert_pending(repo_label="teatree", target_sha="abc")
        self._claimed_task(lease_seconds=-10)  # already-expired lease

        with self._patched():
            result = drain_pending_reinstall()

        assert result.outcome is DrainOutcome.DONE

    def _patched(self):
        from contextlib import ExitStack  # noqa: PLC0415

        stack = ExitStack()
        for cm in _Patches.green():
            stack.enter_context(cm)
        return stack

    def _claimed_task(self, *, lease_seconds: int) -> Task:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="agent-1")
        return Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            claimed_by="agent-1",
            claimed_at=timezone.now(),
            lease_expires_at=timezone.now() + timedelta(seconds=lease_seconds),
        )


class DrainApplyTests(TestCase):
    def setUp(self) -> None:
        self.row = PendingReinstall.objects.upsert_pending(repo_label="teatree", target_sha="abc")

    def test_applies_and_marks_done_when_clean(self) -> None:
        with (
            patch.object(self_update_mod, "reinstall_running_editable", _ok_reinstall),
            patch.object(self_update_mod, "ensure_self_db_migrated", lambda: False),
        ):
            result = drain_pending_reinstall()

        assert result.outcome is DrainOutcome.DONE
        self.row.refresh_from_db()
        assert self.row.state == PendingReinstall.State.DONE
        assert self.row.attempts == 1

    def test_marks_failed_when_reinstall_fails(self) -> None:
        bad = ReinstallResult(ok=False, reinstalled=False, error="reinstall: boom")
        with patch.object(self_update_mod, "reinstall_running_editable", lambda: bad):
            result = drain_pending_reinstall()

        assert result.outcome is DrainOutcome.FAILED
        assert "boom" in result.detail
        self.row.refresh_from_db()
        assert self.row.state == PendingReinstall.State.FAILED
        assert self.row.last_error == "reinstall: boom"

    def test_marks_failed_when_self_db_left_unmigrated(self) -> None:
        with (
            patch.object(self_update_mod, "reinstall_running_editable", _ok_reinstall),
            patch.object(self_update_mod, "ensure_self_db_migrated", lambda: True),
        ):
            result = drain_pending_reinstall()

        assert result.outcome is DrainOutcome.FAILED
        assert "unmigrated" in result.detail
        self.row.refresh_from_db()
        assert self.row.state == PendingReinstall.State.FAILED
