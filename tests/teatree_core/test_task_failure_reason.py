"""A FAILED task names WHY it failed, and that name reaches the operator surfaces (#3957).

``execution_reason`` is why a task was SCHEDULED; before this, nothing recorded why it
FAILED, so a review defect, a lost lease and an exhausted credential rendered identically.
These tests deliberately fail a task through each real failure path and assert the cause is
both recorded (``TaskAttempt.failure_kind`` / ``error``) and surfaced (``tasks list --json``
and the kanban card).
"""

import io
import json
from typing import TYPE_CHECKING, cast

import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.core.management.commands.tasks_session_view import render_tasks_table
from teatree.core.modelkit.task_failure_taxonomy import (
    FailureKind,
    RecoveryStrategy,
    classify_failure,
    is_causeless,
    is_environmental,
    recovery_strategy,
    stall_fingerprints,
    stall_kinds,
)
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.repair_loop import terminal_reason_fingerprint
from teatree.dash.selectors import build_kanban_columns

if TYPE_CHECKING:
    from teatree.core.management.commands.tasks_session_view import TaskRow
    from teatree.dash.selectors import KanbanBoard, KanbanCard


def _ceiling_reason(seconds: int) -> str:
    """The production runtime-ceiling reason (``agents.runner``), which interpolates the breach."""
    return f"stuck_loop: runtime ceiling exceeded: ran {seconds}s without exiting"


def _task(*, phase: str = "reviewing", status: str = Task.Status.PENDING) -> Task:
    ticket = Ticket.objects.create(overlay="test")
    session = Session.objects.create(ticket=ticket, overlay="test")
    return Task.objects.create(
        ticket=ticket,
        session=session,
        phase=phase,
        status=status,
        execution_reason="Auto-scheduled self-PR review (claude:review)",
    )


class TestClassifier(TestCase):
    """The pure vocabulary: every recorded reason resolves to a NAMED kind."""

    def test_lease_loss_is_named_and_environmental(self) -> None:
        kind = classify_failure("stuck_loop: lease lost for task 7: re-claimed by another worker")
        assert kind == FailureKind.LEASE_LOST
        assert is_environmental(kind)

    def test_runtime_ceiling_is_named_and_deterministic(self) -> None:
        """The ceiling shares the ``stuck_loop:`` prefix with the lease loss but is NOT environmental.

        Presenting a ceiling breach as an environment fault invites a requeue straight
        back into the same ceiling — the exact misclassification the headless taxonomy
        was introduced to stop.
        """
        kind = classify_failure("stuck_loop: runtime ceiling exceeded after 3600s")
        assert kind == FailureKind.RUNTIME_CEILING
        assert not is_environmental(kind)

    def test_harness_config_invalid_is_deterministic(self) -> None:
        kind = classify_failure("agent_harness_provider='orca_router_byok' is not valid for agent_harness='claude'")
        assert kind == FailureKind.HARNESS_CONFIG_INVALID
        assert not is_environmental(kind)

    def test_credential_exhaustion_is_environmental(self) -> None:
        kind = classify_failure("all configured Anthropic oauth accounts are exhausted (weekly window)")
        assert kind == FailureKind.CREDENTIAL_EXHAUSTED
        assert is_environmental(kind)

    def test_evidence_refusal_is_deterministic(self) -> None:
        kind = classify_failure("missing required evidence for phase 'reviewing': result must carry a verdict")
        assert kind == FailureKind.EVIDENCE_MISSING
        assert not is_environmental(kind)

    def test_harness_crash_is_environmental(self) -> None:
        kind = classify_failure(
            'Traceback (most recent call last):\n  File "x.py", line 1\n'
            "claude_agent_sdk._errors.ProcessError: Command failed with exit code 1",
        )
        assert kind == FailureKind.HARNESS_CRASH
        assert is_environmental(kind)

    def test_an_unmatched_reason_is_named_unclassified_not_blank(self) -> None:
        """An unrecognised reason still gets a NAME, and is deterministic by default.

        Defaulting to environmental would present an unknown failure as a dismissable
        harness fault, inviting a requeue into the same wall.
        """
        kind = classify_failure("the review found a real defect in the diff")
        assert kind == FailureKind.UNCLASSIFIED
        assert not is_environmental(kind)

    def test_no_reason_at_all_is_named_unrecorded(self) -> None:
        """The bug-detector kind: a failure that recorded nothing is itself nameable."""
        assert classify_failure("") == FailureKind.UNRECORDED
        assert classify_failure("   ") == FailureKind.UNRECORDED

    def test_every_kind_this_vocabulary_names_is_reachable(self) -> None:
        """No kind is decorative — each is produced by some reason, so none silently rots."""
        reasons = [
            "",
            "the review found a real defect in the diff",
            "stuck_loop: lease lost for task 7: re-claimed by another worker",
            "lease_expired: lease expired after 3600s and was reaped",
            "stuck_loop: runtime ceiling exceeded",
            "limit_parked: admission: all_accounts_exhausted",
            "all configured Anthropic oauth accounts are exhausted",
            "agent_harness_provider='x' is not valid for agent_harness='claude'",
            "Traceback (most recent call last):",
            "outage_death: connection refused",
            "result_error: no terminal ResultMessage",
            "provision_failed: uv sync exited 1",
            "landing_unverified: coder yielded with no commit",
            "no_result_envelope: agent produced no JSON result envelope",
            "missing required evidence for phase 'reviewing'",
            "review verdict recording refused: merge_safe needs a reviewed sha",
            "cancelled: operator cancelled the task",
            "superseded: ticket reworked",
            "agent_abandoned: agent failed the task without giving a reason",
        ]
        assert {classify_failure(r) for r in reasons} == set(FailureKind.values)

    def test_the_requeue_transient_set_is_always_environmental(self) -> None:
        """The one-way drift guard against the requeue vocabulary (see module docstring).

        Everything the requeue sweep treats as transient MUST also read as environmental
        here, so the card can never tell an operator "your code is broken" about a failure
        the sweep is quietly retrying as infrastructure.
        """
        transient_reasons = [
            "outage_death: connection refused",
            "result_error: no terminal ResultMessage",
            "provision_failed: uv sync exited 1",
            "landing_unverified: coder yielded with no commit",
            "Unable to connect to API",
        ]
        for reason in transient_reasons:
            assert recovery_strategy(classify_failure(reason)) is RecoveryStrategy.RETRY, reason
            assert is_environmental(classify_failure(reason)), reason


class TestCauselessKinds:
    """#4075: a failure that named no cause must not be compared against itself.

    Membership is the absence-of-a-cause test, not fingerprint collision:
    ``no_result_envelope``'s constant reason self-collides, ``runtime_ceiling``'s
    interpolated one does not. Without the drop, "we learned nothing, twice" reads as
    "one defect recurred twice" and halts a phase that was never doomed.
    """

    def test_the_two_reporting_failures_are_causeless(self) -> None:
        assert is_causeless(classify_failure("no_result_envelope: agent produced no JSON result envelope"))
        assert is_causeless(classify_failure("stuck_loop: runtime ceiling exceeded after 3600s"))

    def test_a_named_defect_is_not_causeless(self) -> None:
        # The control the whole change rests on: the stall check must still be able to fire.
        assert not is_causeless(classify_failure("missing required evidence for phase 'coding'"))
        assert not is_causeless(classify_failure("AssertionError: expected 3 got 4"))

    def test_an_unnamed_kind_is_not_causeless(self) -> None:
        # ``unclassified``/``unrecorded`` also fail to NAME a cause, but their free text
        # differs, so the fingerprint check discriminates them and is deliberately kept.
        assert not is_causeless(FailureKind.UNCLASSIFIED)
        assert not is_causeless(FailureKind.UNRECORDED)

    def test_causeless_fingerprints_are_dropped_from_the_stall_comparison(self) -> None:
        fingerprint = terminal_reason_fingerprint("no_result_envelope: agent produced no JSON result envelope")
        kind = classify_failure("no_result_envelope: agent produced no JSON result envelope")
        assert stall_fingerprints([(kind, fingerprint), (kind, fingerprint)]) == []

    def test_a_named_defects_fingerprints_still_count(self) -> None:
        reason = "missing required evidence for phase 'coding'"
        fingerprint = terminal_reason_fingerprint(reason)
        kind = classify_failure(reason)
        assert stall_fingerprints([(kind, fingerprint), (kind, fingerprint)]) == [fingerprint, fingerprint]

    def test_an_empty_fingerprint_is_dropped_as_before(self) -> None:
        assert stall_fingerprints([(FailureKind.UNCLASSIFIED, ""), (FailureKind.UNCLASSIFIED, "fp")]) == ["fp"]

    def test_runtime_ceiling_reasons_never_collide_so_the_fingerprint_filter_has_nothing_to_drop(self) -> None:
        # The fact the corrected #4075 prose rests on: ``\b\d+\b`` has no word boundary
        # before the ``s``, so the breach survives normalization.
        assert terminal_reason_fingerprint(_ceiling_reason(3601)) != terminal_reason_fingerprint(_ceiling_reason(3722))

    def test_the_collision_assertion_can_detect_a_collision(self) -> None:
        # Control for the assertion above: the same two counts written as bare words ARE
        # masked, so it is a real discrimination and not a hash that differs on everything.
        bare = "stuck_loop: runtime ceiling exceeded: ran {} seconds without exiting"
        assert terminal_reason_fingerprint(bare.format(3601)) == terminal_reason_fingerprint(bare.format(3722))


class TestStallKinds:
    """#4276: the KIND-side stall filter — the mechanism ``runtime_ceiling`` actually needs.

    ``no_result_envelope``'s reason is a module constant, so it self-collides and the
    fingerprint filter drops it even with this clause gone. ``runtime_ceiling``'s
    interpolates the breach and never collides, so this drop is its whole mechanism.
    """

    def test_a_causeless_kind_is_dropped(self) -> None:
        assert stall_kinds([FailureKind.RUNTIME_CEILING, FailureKind.RUNTIME_CEILING]) == []
        assert stall_kinds([FailureKind.NO_RESULT_ENVELOPE, FailureKind.NO_RESULT_ENVELOPE]) == []

    def test_a_named_deterministic_kind_still_counts(self) -> None:
        # The control the whole filter rests on: the named-cause stall must still fire.
        kinds = [FailureKind.EVIDENCE_MISSING, FailureKind.EVIDENCE_MISSING]
        assert stall_kinds(kinds) == kinds

    def test_an_environmental_kind_is_dropped(self) -> None:
        assert stall_kinds([FailureKind.OUTAGE, FailureKind.LEASE_LOST]) == []

    def test_an_unnamed_kind_is_dropped(self) -> None:
        assert stall_kinds([FailureKind.UNCLASSIFIED, FailureKind.UNRECORDED]) == []

    def test_a_blank_kind_is_dropped(self) -> None:
        assert stall_kinds(["", FailureKind.EVIDENCE_MISSING]) == [FailureKind.EVIDENCE_MISSING]

    def test_a_dropped_kind_leaves_the_survivors_in_order(self) -> None:
        # Dropping rather than substituting a placeholder is what breaks a run: the two
        # named kinds either side of a causeless one are not adjacent, so they never stall.
        kinds = [FailureKind.RECORDING_REFUSED, FailureKind.RUNTIME_CEILING, FailureKind.EVIDENCE_MISSING]
        assert stall_kinds(kinds) == [FailureKind.RECORDING_REFUSED, FailureKind.EVIDENCE_MISSING]


class TestAttemptStampsFailureKind(TestCase):
    """The named kind is PERSISTED on the attempt, not re-derived by each reader."""

    def test_failed_attempt_carries_the_named_kind(self) -> None:
        task = _task()
        attempt = TaskAttempt.objects.create(
            task=task,
            ended_at=timezone.now(),
            exit_code=1,
            error="stuck_loop: lease lost for task 1: re-claimed by another worker",
        )
        attempt.refresh_from_db()
        assert attempt.failure_kind == FailureKind.LEASE_LOST

    def test_clean_attempt_carries_no_failure_kind(self) -> None:
        task = _task()
        attempt = TaskAttempt.objects.create(
            task=task,
            ended_at=timezone.now(),
            exit_code=0,
        )
        attempt.refresh_from_db()
        assert attempt.failure_kind == ""


class TestNoFailurePathRecordsNothing(TestCase):
    """Every way a task reaches FAILED leaves a named reason behind."""

    def test_fail_requires_a_reason_and_records_it(self) -> None:
        task = _task(status=Task.Status.CLAIMED)
        task.fail(reason="mcp task_fail: agent abandoned the task")
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.failure_reason == "mcp task_fail: agent abandoned the task"
        assert task.failure_kind != ""

    def test_fail_without_a_reason_is_a_programming_error(self) -> None:
        """The structural guarantee: a new failure path CANNOT record nothing."""
        task = _task(status=Task.Status.CLAIMED)
        with pytest.raises(TypeError):
            task.fail()  # ty: ignore[missing-argument] — the missing argument IS the assertion: `pytest.raises(TypeError)`.

    def test_lease_reaper_records_why_each_row_was_failed(self) -> None:
        """``reap_stale_claims`` bulk-UPDATEd rows to FAILED, recording no attempt and no reason."""
        task = _task(status=Task.Status.CLAIMED)
        Task.objects.filter(pk=task.pk).update(
            claimed_by="worker-1",
            claimed_at=timezone.now() - timezone.timedelta(hours=2),
            lease_expires_at=timezone.now() - timezone.timedelta(hours=1),
        )

        reaped = Task.objects.reap_stale_claims()

        assert reaped == 1
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.failure_kind == FailureKind.LEASE_EXPIRED
        assert "lease" in task.failure_reason.lower()
        assert task.attempts.filter(error__gt="").exists()

    def test_cancel_without_an_explicit_reason_still_records_one(self) -> None:
        task = _task(status=Task.Status.PENDING)

        call_command("tasks", "cancel", str(task.pk), "--confirm")

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.failure_reason != ""
        assert task.failure_kind == FailureKind.CANCELLED

    def test_rework_cancellation_records_a_named_reason(self) -> None:
        task = _task(status=Task.Status.PENDING)

        task.ticket._cancel_pending_tasks()

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.failure_kind == FailureKind.SUPERSEDED
        assert task.failure_reason != ""


class TestFailureReachesTheOperatorSurfaces(TestCase):
    """RED anchor: with the recording removed the cause is ABSENT from both surfaces."""

    def test_tasks_list_json_carries_the_failure_reason_and_kind(self) -> None:
        task = _task(status=Task.Status.CLAIMED)
        task.complete_with_attempt(
            exit_code=1,
            error="stuck_loop: lease lost for task 1: re-claimed by another worker",
        )

        out = _tasks_list_json()
        row = next(r for r in out if r["task_id"] == task.pk)

        assert row["status"] == "failed"
        assert row["failure_kind"] == FailureKind.LEASE_LOST
        assert "lease lost" in row["failure_reason"]
        assert row["failure_environmental"] is True
        # The failure reason is DISTINGUISHABLE from the scheduling reason.
        assert row["failure_reason"] != row["execution_reason"]
        assert "self-PR review" in row["execution_reason"]

    def test_tasks_list_json_marks_a_deterministic_failure_non_environmental(self) -> None:
        task = _task(status=Task.Status.CLAIMED)
        task.complete_with_attempt(exit_code=1, error="stuck_loop: runtime ceiling exceeded after 3600s")

        row = next(r for r in _tasks_list_json() if r["task_id"] == task.pk)

        assert row["failure_kind"] == FailureKind.RUNTIME_CEILING
        assert row["failure_environmental"] is False

    def test_a_healthy_task_carries_no_failure_fields(self) -> None:
        task = _task()

        row = next(r for r in _tasks_list_json() if r["task_id"] == task.pk)

        assert row["failure_kind"] == ""
        assert row["failure_reason"] == ""
        assert row["failure_environmental"] is False

    def test_the_human_task_table_shows_the_named_cause(self) -> None:
        """The human listing is a surface too — it showed only the SCHEDULING reason."""
        task = _task(status=Task.Status.CLAIMED)
        task.complete_with_attempt(exit_code=1, error="stuck_loop: lease lost for task 1: re-claimed by another")
        rows = cast("list[TaskRow]", call_command("tasks", "list"))

        buf = io.StringIO()
        render_tasks_table(rows, stream=buf)
        rendered = buf.getvalue()

        assert "Failed because" in rendered
        assert FailureKind.LEASE_LOST in rendered
        # And the scheduling reason is still shown, in its own column.
        assert "self-PR review" in rendered

    def test_the_kanban_card_shows_the_named_kind(self) -> None:

        task = _task(status=Task.Status.CLAIMED)
        task.complete_with_attempt(
            exit_code=1,
            error="agent_harness_provider='orca_router_byok' is not valid for agent_harness='claude'",
        )

        card = _find_card(build_kanban_columns(), task.ticket_id)

        assert card.failure_kind == FailureKind.HARNESS_CONFIG_INVALID
        assert card.failure_environmental is False
        assert "orca_router_byok" in card.last_error


def _tasks_list_json() -> list[dict[str, object]]:
    """The task-listing rows, round-tripped through JSON as ``tasks list --json`` emits them."""
    rows = cast("list[dict[str, object]]", call_command("tasks", "list"))
    return cast("list[dict[str, object]]", json.loads(json.dumps(rows)))


def _find_card(board: "KanbanBoard", ticket_id: int) -> "KanbanCard":
    for group in board.groups:
        for column in group.columns:
            for card in column.cards:
                if card.ticket_id == ticket_id:
                    return card
    msg = f"no card for ticket {ticket_id}"
    raise AssertionError(msg)
