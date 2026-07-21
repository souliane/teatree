"""``acquire_or_enqueue`` — the reap → retry → enqueue acquisition path (#2190, #44).

Replaces the old ``refuse_if_limit_exceeded`` ``SystemExit(1)`` at the
``worktree start`` / ``workspace start`` boundary. When the cap is hit it
(1) reaps idle stacks, (2) re-checks the limit, and (3) if still full ENQUEUES
a ``LocalStackQueueItem`` and returns ``False`` (the caller must NOT advance
the FSM) — never ``SystemExit``. When a slot is free it returns ``True``.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.gates import local_stack_gate as gate_mod
from teatree.core.gates.local_stack_gate import LocalStackLimitExceededError, acquire_or_enqueue
from teatree.core.gates.provision_admission_gate import ProvisionAdmissionVerdict
from teatree.core.models import LocalStackQueueItem, Ticket, Worktree


def _worktree(*, overlay: str = "t3-heavy", ticket_number: str, state: Worktree.State) -> Worktree:
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
    )


class TestAcquireWhenSlotFree(TestCase):
    """No blocker → acquire returns True, enqueues nothing."""

    def test_returns_true_and_enqueues_nothing(self) -> None:
        candidate = _worktree(ticket_number="500", state=Worktree.State.PROVISIONED)
        messages: list[str] = []
        with patch.object(gate_mod, "resolve_max_concurrent_local_stacks", return_value=1):
            acquired = acquire_or_enqueue(candidate, write_out=messages.append)
        assert acquired is True
        assert LocalStackQueueItem.objects.count() == 0


class TestEnqueueWhenStillFull(TestCase):
    """Cap hit, no idle to reap → enqueue + return False, never SystemExit."""

    def test_enqueues_and_returns_false_without_systemexit(self) -> None:
        blocker = _worktree(ticket_number="510", state=Worktree.State.SERVICES_UP)
        candidate = _worktree(ticket_number="511", state=Worktree.State.PROVISIONED)
        messages: list[str] = []
        with (
            patch.object(gate_mod, "resolve_max_concurrent_local_stacks", return_value=1),
            patch.object(gate_mod, "_running_container_count", return_value=1),
            patch.object(gate_mod, "reap_idle_stacks", return_value=0),
        ):
            acquired = acquire_or_enqueue(candidate, write_out=messages.append)
        assert acquired is False
        # A queued row exists for the candidate (no SystemExit was raised).
        item = LocalStackQueueItem.objects.get(worktree=candidate)
        assert item.status == LocalStackQueueItem.Status.QUEUED
        assert any("queued" in m.lower() for m in messages)
        del blocker

    def test_does_not_raise_system_exit(self) -> None:
        candidate = _worktree(ticket_number="512", state=Worktree.State.PROVISIONED)
        _worktree(ticket_number="513", state=Worktree.State.SERVICES_UP)
        with (
            patch.object(gate_mod, "resolve_max_concurrent_local_stacks", return_value=1),
            patch.object(gate_mod, "_running_container_count", return_value=1),
            patch.object(gate_mod, "reap_idle_stacks", return_value=0),
        ):
            # The whole point of #2190: NO SystemExit — proven by it not raising.
            result = acquire_or_enqueue(candidate, write_out=lambda _m: None)
        assert result is False


class TestRamAwareAdmission(TestCase):
    """#2949: on a capped overlay, hold a new stack when host RAM is over the ceiling."""

    def test_ram_over_ceiling_enqueues_even_with_a_free_count_slot(self) -> None:
        candidate = _worktree(ticket_number="600", state=Worktree.State.PROVISIONED)
        messages: list[str] = []
        with (
            patch.object(gate_mod, "resolve_max_concurrent_local_stacks", return_value=1),
            patch.object(gate_mod, "check_provision_admission", return_value=ProvisionAdmissionVerdict.hold("ram")),
        ):
            acquired = acquire_or_enqueue(candidate, write_out=messages.append)
        assert acquired is False
        item = LocalStackQueueItem.objects.get(worktree=candidate)
        assert item.status == LocalStackQueueItem.Status.QUEUED
        assert any("queued" in m.lower() for m in messages)

    def test_ram_ok_proceeds_to_the_count_gate(self) -> None:
        candidate = _worktree(ticket_number="601", state=Worktree.State.PROVISIONED)
        with (
            patch.object(gate_mod, "resolve_max_concurrent_local_stacks", return_value=1),
            patch.object(gate_mod, "check_provision_admission", return_value=ProvisionAdmissionVerdict.allow()),
        ):
            acquired = acquire_or_enqueue(candidate, write_out=lambda _m: None)
        assert acquired is True
        assert LocalStackQueueItem.objects.count() == 0

    def test_unbounded_overlay_never_ram_holds(self) -> None:
        """The default (unbounded) overlay keeps its pre-#2949 behaviour — RAM is not consulted."""
        candidate = _worktree(ticket_number="602", state=Worktree.State.PROVISIONED)
        with (
            patch.object(gate_mod, "resolve_max_concurrent_local_stacks", return_value=0),
            patch.object(gate_mod, "check_provision_admission", return_value=ProvisionAdmissionVerdict.hold("ram")),
        ):
            acquired = acquire_or_enqueue(candidate, write_out=lambda _m: None)
        assert acquired is True
        assert LocalStackQueueItem.objects.count() == 0


class TestReapThenAcquire(TestCase):
    """Cap hit but a reap frees a slot → acquire returns True (root-cause fix)."""

    def test_reap_frees_a_slot_so_acquisition_succeeds(self) -> None:
        blocker = _worktree(ticket_number="520", state=Worktree.State.SERVICES_UP)
        candidate = _worktree(ticket_number="521", state=Worktree.State.PROVISIONED)

        def _reap(*, overlay: str, write_out: object = None) -> int:
            # Model the reaper demoting the blocker out of a running state.
            blocker.state = Worktree.State.PROVISIONED
            blocker.save(update_fields=["state"])
            return 1

        with (
            patch.object(gate_mod, "resolve_max_concurrent_local_stacks", return_value=1),
            patch.object(gate_mod, "_running_container_count", return_value=1),
            patch.object(gate_mod, "reap_idle_stacks", side_effect=_reap),
        ):
            acquired = acquire_or_enqueue(candidate, write_out=lambda _m: None)
        assert acquired is True
        assert LocalStackQueueItem.objects.count() == 0


class TestNoneCandidate(TestCase):
    """An empty workspace (no worktrees) acquires trivially."""

    def test_none_candidate_returns_true(self) -> None:
        assert acquire_or_enqueue(None, write_out=lambda _m: None) is True


class TestEnqueueIsIdempotent(TestCase):
    """Re-firing acquire while already queued does not stack duplicate rows."""

    def test_second_enqueue_reuses_row(self) -> None:
        candidate = _worktree(ticket_number="530", state=Worktree.State.PROVISIONED)
        _worktree(ticket_number="531", state=Worktree.State.SERVICES_UP)
        with (
            patch.object(gate_mod, "resolve_max_concurrent_local_stacks", return_value=1),
            patch.object(gate_mod, "_running_container_count", return_value=1),
            patch.object(gate_mod, "reap_idle_stacks", return_value=0),
        ):
            acquire_or_enqueue(candidate, write_out=lambda _m: None)
            acquire_or_enqueue(candidate, write_out=lambda _m: None)
        assert LocalStackQueueItem.objects.filter(worktree=candidate).count() == 1


class TestReapIdleStacksHelper(TestCase):
    """``reap_idle_stacks`` demotes each reapable worktree and returns the count."""

    def test_demotes_reapable_and_returns_count(self) -> None:
        from teatree.core.gates.local_stack_gate import reap_idle_stacks  # noqa: PLC0415

        wt = _worktree(ticket_number="550", state=Worktree.State.SERVICES_UP)
        messages: list[str] = []
        with (
            patch.object(gate_mod, "_running_container_count", return_value=1),
            patch(
                "teatree.core.gates.idle_stack.reapable_worktrees",
                return_value=[wt],
            ),
        ):
            count = reap_idle_stacks(overlay="t3-heavy", write_out=messages.append)
        wt.refresh_from_db()
        assert count == 1
        assert wt.state == Worktree.State.PROVISIONED
        assert any("Reaped" in m for m in messages)

    def test_returns_zero_when_nothing_reapable(self) -> None:
        from teatree.core.gates.local_stack_gate import reap_idle_stacks  # noqa: PLC0415

        with patch("teatree.core.gates.idle_stack.reapable_worktrees", return_value=[]):
            assert reap_idle_stacks(overlay="t3-heavy") == 0

    def test_reaps_without_write_out_callback(self) -> None:
        """The default ``write_out=None`` path still demotes (no notice printed)."""
        from teatree.core.gates.local_stack_gate import reap_idle_stacks  # noqa: PLC0415

        wt = _worktree(ticket_number="570", state=Worktree.State.SERVICES_UP)
        with (
            patch.object(gate_mod, "_running_container_count", return_value=1),
            patch("teatree.core.gates.idle_stack.reapable_worktrees", return_value=[wt]),
        ):
            assert reap_idle_stacks(overlay="t3-heavy") == 1
        wt.refresh_from_db()
        assert wt.state == Worktree.State.PROVISIONED

    def test_skips_when_locked_row_cannot_stop(self) -> None:
        """A reapable worktree whose locked row can't ``stop_services`` is skipped."""
        from teatree.core.gates.local_stack_gate import reap_idle_stacks  # noqa: PLC0415

        wt = _worktree(ticket_number="560", state=Worktree.State.SERVICES_UP)
        with (
            patch("teatree.core.gates.idle_stack.reapable_worktrees", return_value=[wt]),
            patch.object(gate_mod, "can_proceed", return_value=False),
        ):
            assert reap_idle_stacks(overlay="t3-heavy") == 0
        wt.refresh_from_db()
        assert wt.state == Worktree.State.SERVICES_UP


class TestReapScopedByTicketOverlay(TestCase):
    """F2.11: the reap is scoped by the TICKET's overlay, not the (possibly blank) Worktree.overlay.

    A candidate row auto-detected via cwd can carry an empty ``Worktree.overlay``
    while its ticket carries the real overlay; reaping by ``candidate.overlay``
    alone would target the wrong (empty) overlay, free nothing, and needlessly
    re-enqueue. The reap must use ``candidate.ticket.overlay or candidate.overlay``
    — matching the cap-count scoping in ``check_local_stack_limit``.
    """

    def test_reap_uses_ticket_overlay_when_worktree_overlay_blank(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-heavy", issue_url="https://example.com/t3-heavy/issues/700")
        candidate = Worktree.objects.create(
            overlay="",  # blank — auto-detected via cwd
            ticket=ticket,
            repo_path="backend",
            branch="700-feat",
            state=Worktree.State.PROVISIONED,
        )
        blocker_ticket = Ticket.objects.create(overlay="t3-heavy", issue_url="https://example.com/t3-heavy/issues/701")
        Worktree.objects.create(
            overlay="t3-heavy",
            ticket=blocker_ticket,
            repo_path="backend",
            branch="701-feat",
            state=Worktree.State.SERVICES_UP,
        )

        captured: dict[str, object] = {}

        def _reap(*, overlay: str, write_out: object = None) -> int:
            captured["overlay"] = overlay
            return 0

        with (
            patch.object(gate_mod, "resolve_max_concurrent_local_stacks", return_value=1),
            patch.object(gate_mod, "_running_container_count", return_value=1),
            patch.object(gate_mod, "reap_idle_stacks", side_effect=_reap),
        ):
            acquire_or_enqueue(candidate, write_out=lambda _m: None)

        # Scoped to the ticket's real overlay, NOT the blank Worktree.overlay ("").
        assert captured["overlay"] == "t3-heavy"


class TestCheckLimitUntouched(TestCase):
    """``check_local_stack_limit`` keeps its SystemExit-free refusal contract."""

    def test_check_still_raises_limit_error(self) -> None:
        from teatree.core.gates.local_stack_gate import check_local_stack_limit  # noqa: PLC0415

        _worktree(ticket_number="540", state=Worktree.State.SERVICES_UP)
        candidate = _worktree(ticket_number="541", state=Worktree.State.PROVISIONED)
        with (
            patch.object(gate_mod, "_running_container_count", return_value=1),
            pytest.raises(LocalStackLimitExceededError),
        ):
            check_local_stack_limit(candidate, limit=1)
