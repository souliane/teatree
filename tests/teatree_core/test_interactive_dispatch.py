"""In-session interactive dispatch seams.

Covers phase tasks defaulting INTERACTIVE, the ``record-attempt`` hand-off,
and the fail-closed headless-dispatch guard.

Under ``agent_runtime=interactive`` (the default), loop-dispatched phase work
runs as in-session sub-agents. These tests pin three load-bearing seams:
``Task.save`` routes a freshly-created loop-dispatched phase task to INTERACTIVE
(leaving free-form headless work HEADLESS); ``tasks record-attempt`` records an
in-session sub-agent's result envelope through the SHARED recorder; and
``execute_headless_task`` refuses to dispatch a loop-dispatched phase headless
while ``agent_runtime=interactive`` (a headless runtime lifts the refusal).
"""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket

IMMEDIATE_BACKEND = {
    "TASKS": {"default": {"BACKEND": "django_tasks.backends.immediate.ImmediateBackend"}},
}


class TestPhaseTaskDefaultsInteractive(TestCase):
    def setUp(self) -> None:
        super().setUp()
        ConfigSetting.objects.set_value("agent_runtime", "interactive")

    def _author_ticket(self) -> Ticket:
        return Ticket.objects.create(role=Ticket.Role.AUTHOR)

    def test_loop_dispatched_phase_defaults_to_interactive(self) -> None:
        ticket = self._author_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="coding")

        task = Task.objects.create(ticket=ticket, session=session, phase="coding")

        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert "agent_runtime=interactive" in task.execution_reason

    def test_loop_dispatched_phase_is_headless_under_sdk_runtime(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "sdk_oauth")
        ticket = self._author_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="coding")

        task = Task.objects.create(ticket=ticket, session=session, phase="coding")

        assert task.execution_target == Task.ExecutionTarget.HEADLESS

    def test_short_verb_phase_also_routes_interactive(self) -> None:
        ticket = self._author_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="coding")

        task = Task.objects.create(ticket=ticket, session=session, phase="code")

        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE

    def test_free_form_phase_stays_headless(self) -> None:
        ticket = self._author_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="x")

        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="architectural_review",
            execution_target=Task.ExecutionTarget.HEADLESS,
        )

        assert task.execution_target == Task.ExecutionTarget.HEADLESS

    def test_explicit_reason_is_preserved(self) -> None:
        ticket = self._author_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="coding")

        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            execution_reason="hand-written",
        )

        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert task.execution_reason == "hand-written"

    def test_reroute_to_headless_after_creation_is_not_overridden(self) -> None:
        ticket = self._author_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")

        task.route_to_headless(reason="deliberate")

        task.refresh_from_db()
        assert task.execution_target == Task.ExecutionTarget.HEADLESS

    def test_reviewer_role_reviewing_routes_interactive(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.REVIEWER)
        session = Session.objects.create(ticket=ticket, agent_id="review")

        task = Task.objects.create(ticket=ticket, session=session, phase="reviewing")

        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE


class TestRecordAttemptCommand(TestCase):
    def _claimed_task(self, *, phase: str = "coding") -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.PLANNED)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        task.claim(claimed_by="loop-slot")
        return task

    def test_records_success_envelope_and_completes_task(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps({"summary": "done", "files_modified": [{"path": "a.py", "action": "modified"}]})
        out = StringIO()

        call_command("tasks", "record-attempt", str(task.pk), result_json, agent_session_id="sess-1", stdout=out)

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        attempt = task.attempts.latest("pk")
        assert attempt.result["summary"] == "done"
        assert attempt.agent_session_id == "sess-1"
        assert task.ticket.state == Ticket.State.CODED

    def test_outage_death_fails_task_without_advancing_ticket(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps(
            {"summary": "Unable to connect to API", "files_modified": [{"path": "a.py", "action": "modified"}]},
        )

        call_command("tasks", "record-attempt", str(task.pk), result_json, stdout=StringIO())

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.ticket.state == Ticket.State.PLANNED
        assert task.attempts.latest("pk").error.startswith("outage_death:")

    def test_missing_phase_evidence_fails_task(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps({"summary": "no files changed"})

        # A valid-but-evidence-light envelope is recorded as a FAILED attempt
        # (a clean refusal, not a CLI error), so the command exits 0.
        call_command("tasks", "record-attempt", str(task.pk), result_json, stdout=StringIO())

        task.refresh_from_db()
        # coding requires files_modified evidence (#1284) → fail, not complete.
        assert task.status == Task.Status.FAILED

    def test_needs_user_input_completes_and_schedules_followup(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps({"summary": "blocked", "needs_user_input": True, "user_input_reason": "design call"})
        call_command("tasks", "record-attempt", str(task.pk), result_json, stdout=StringIO())

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        followup = task.child_tasks.get()
        assert followup.execution_target == Task.ExecutionTarget.INTERACTIVE
        assert followup.phase == "coding"

    def test_invalid_json_rejected(self) -> None:
        task = self._claimed_task()
        with pytest.raises(SystemExit):
            call_command("tasks", "record-attempt", str(task.pk), "not json", stderr=StringIO())
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED

    def test_unexpected_keys_fail_task(self) -> None:
        task = self._claimed_task()
        result_json = json.dumps({"summary": "ok", "bogus": 1})
        call_command("tasks", "record-attempt", str(task.pk), result_json, stdout=StringIO())
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_unclaimed_task_rejected(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        # PENDING (not claimed) — recording must be refused.
        with pytest.raises(SystemExit):
            call_command("tasks", "record-attempt", str(task.pk), json.dumps({"summary": "x"}), stderr=StringIO())
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_finished_task_rejected(self) -> None:
        task = self._claimed_task()
        task.complete()
        with pytest.raises(SystemExit):
            call_command("tasks", "record-attempt", str(task.pk), json.dumps({"summary": "x"}), stderr=StringIO())


class TestHeadlessDispatchGuard(TestCase):
    """``execute_headless_task`` refuses metered dispatch for loop phases."""

    def _headless_loop_task(self) -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        # Force HEADLESS past the save() invariant to simulate a stray enqueue.
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        Task.objects.filter(pk=task.pk).update(execution_target=Task.ExecutionTarget.HEADLESS)
        task.refresh_from_db()
        return task

    def test_refuses_and_records_routing_error_under_interactive(self) -> None:
        from teatree.core.tasks import execute_headless_task  # noqa: PLC0415

        ConfigSetting.objects.set_value("agent_runtime", "interactive")
        task = self._headless_loop_task()
        out = execute_headless_task.call(task.pk, task.phase)

        assert out["exit_code"] == 1
        assert "routing_error" in out
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        attempt = task.attempts.latest("pk")
        assert "routing_error" in attempt.result

    def test_headless_runtime_allows_dispatch_to_proceed(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        import teatree.core.tasks as tasks_mod  # noqa: PLC0415

        ConfigSetting.objects.set_value("agent_runtime", "sdk_oauth")
        task = self._headless_loop_task()
        sentinel = TaskAttempt(exit_code=0, result={"summary": "ran"})

        with (
            patch("teatree.core.headless_dispatch._runner", return_value=sentinel) as run,
            patch("teatree.core.overlay_loader.get_overlay") as overlay,
        ):
            overlay.return_value.metadata.get_skill_metadata.return_value = {}
            tasks_mod.execute_headless_task.call(task.pk, task.phase)

        assert run.called

    def test_free_form_headless_phase_still_dispatches(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        import teatree.core.tasks as tasks_mod  # noqa: PLC0415

        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR)
        session = Session.objects.create(ticket=ticket, agent_id="x")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="architectural_review",
            execution_target=Task.ExecutionTarget.HEADLESS,
        )
        sentinel = TaskAttempt(exit_code=0, result={"summary": "ran"})

        with (
            patch("teatree.core.headless_dispatch._runner", return_value=sentinel) as run,
            patch("teatree.core.overlay_loader.get_overlay") as overlay,
        ):
            overlay.return_value.metadata.get_skill_metadata.return_value = {}
            tasks_mod.execute_headless_task.call(task.pk, task.phase)

        assert run.called
