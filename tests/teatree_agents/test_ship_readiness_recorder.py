"""A shipping run on the read-only executor returns a verdict; only a ready one reaches the worker's ship job."""

from unittest.mock import patch

from django.test import TestCase

from teatree.agents.attempt_recorder import AttemptUsage, record_result_envelope
from teatree.agents.envelope_contract import envelope_contract_lines
from teatree.agents.result_schema import required_evidence_for_phase
from teatree.agents.ship_readiness_recorder import NOT_READY_PREFIX, record_returned_ship_readiness
from teatree.core.models import Session, Task, Ticket


class _Shipper:
    def __init__(self, outcome: object = None) -> None:
        self.outcome = {"queued": True, "warning": "queued"} if outcome is None else outcome
        self.shipped: list[str] = []

    def __call__(self, ticket_id: str) -> object:
        self.shipped.append(ticket_id)
        return self.outcome


class TestOnlyAReadyVerdictShips(TestCase):
    def setUp(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.SELF_REVIEWED)
        self.task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="shipping")
        self.shipper = _Shipper()

    def _record(self, verdict: object) -> str:
        return record_returned_ship_readiness(
            self.task, {"ship_readiness": verdict}, phase="shipping", shipper=self.shipper
        )

    def test_a_ready_verdict_runs_the_workers_ship_for_the_task_ticket(self) -> None:
        assert self._record({"ready": True}) == ""
        assert self.shipper.shipped == [str(self.task.ticket_id)]

    def test_a_verdict_that_is_not_ready_ships_nothing_and_names_its_reasons(self) -> None:
        refusal = self._record({"ready": False, "reasons": ["no commits ahead"]})

        assert refusal == f"{NOT_READY_PREFIX}the executor judged the branch not ready: no commits ahead"
        assert self.shipper.shipped == []

    def test_a_verdict_without_a_true_ready_ships_nothing(self) -> None:
        for verdict in ({}, {"ready": "yes"}, {"ready": 1}, "ready", None):
            with self.subTest(verdict=verdict):
                assert self._record(verdict).startswith(NOT_READY_PREFIX)
        assert self.shipper.shipped == []

    def test_a_gate_refusal_is_the_task_failure_not_a_queued_ship(self) -> None:
        self.shipper.outcome = {"allowed": False, "error": "shipping requires: reviewing", "missing": ["reviewing"]}

        assert self._record({"ready": True}) == (
            f"{NOT_READY_PREFIX}the ship gates refused it: shipping requires: reviewing"
        )

    def test_a_ready_verdict_returned_while_stopping_for_the_owner_ships_nothing(self) -> None:
        result = {"needs_user_input": True, "ship_readiness": {"ready": True}}

        assert record_returned_ship_readiness(self.task, result, phase="shipping", shipper=self.shipper) == ""
        assert self.shipper.shipped == []

    def test_another_phase_or_an_absent_verdict_is_a_no_op(self) -> None:
        assert record_returned_ship_readiness(self.task, {}, phase="shipping", shipper=self.shipper) == ""
        assert (
            record_returned_ship_readiness(
                self.task, {"ship_readiness": {"ready": True}}, phase="coding", shipper=self.shipper
            )
            == ""
        )
        assert self.shipper.shipped == []


class TestTheRecorderEntryPoint(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.SELF_REVIEWED)
        self.task = Task.objects.create(
            ticket=self.ticket, session=Session.objects.create(ticket=self.ticket), phase="shipping"
        )
        self.task.claim(claimed_by="loop-slot")

    def _record(self, result: dict[str, object], harness: str = "codex_app_server") -> object:
        return record_result_envelope(self.task, result, phase="shipping", usage=AttemptUsage(selected_harness=harness))

    def test_a_not_ready_executor_run_fails_the_task_and_ships_nothing(self) -> None:
        with patch("teatree.agents.ship_readiness_recorder.call_command_streamed") as ship:
            attempt = self._record({"summary": "not yet", "ship_readiness": {"ready": False, "reasons": ["red CI"]}})

        self.task.refresh_from_db()
        assert (self.task.status, attempt.error.startswith(NOT_READY_PREFIX)) == (Task.Status.FAILED, True)
        ship.assert_not_called()

    def test_a_ready_executor_run_hands_the_push_to_the_workers_pr_create(self) -> None:
        def pr_create(*_args: object, **_kwargs: object) -> dict[str, object]:
            Ticket.objects.filter(pk=self.ticket.pk).update(state=Ticket.State.PR_OPENED)
            return {"queued": True}

        with patch("teatree.agents.ship_readiness_recorder.call_command_streamed", side_effect=pr_create) as ship:
            attempt = self._record({"summary": "ready", "ship_readiness": {"ready": True}})

        assert attempt.error == ""
        assert [call.args[:3] for call in ship.call_args_list] == [("pr", "create", str(self.ticket.pk))]

    def test_an_executor_run_with_no_verdict_is_refused(self) -> None:
        attempt = self._record({"summary": "pushed", "commands_executed": ["git push"]})

        assert "must return `ship_readiness`" in attempt.error

    def test_a_claude_run_carrying_only_a_ship_readiness_verdict_is_refused_and_ships_nothing(self) -> None:
        with patch("teatree.agents.ship_readiness_recorder.call_command_streamed") as ship:
            attempt = self._record({"summary": "did nothing", "ship_readiness": {"ready": True}}, "claude_sdk")

        assert "missing required evidence for phase 'shipping'" in attempt.error
        assert "[commands_executed]" in attempt.error
        ship.assert_not_called()

    def test_a_claude_run_never_runs_pr_create_for_a_verdict_it_also_returns(self) -> None:
        Ticket.objects.filter(pk=self.ticket.pk).update(state=Ticket.State.PR_OPENED)
        with patch("teatree.agents.ship_readiness_recorder.call_command_streamed") as ship:
            attempt = self._record(
                {"summary": "shipped", "commands_executed": ["t3 pr create 1"], "ship_readiness": {"ready": True}},
                "claude_sdk",
            )

        assert attempt.error == ""
        ship.assert_not_called()

    def test_a_claude_run_still_records_on_commands_executed(self) -> None:
        Ticket.objects.filter(pk=self.ticket.pk).update(state=Ticket.State.PR_OPENED)
        with patch("teatree.agents.ship_readiness_recorder.call_command_streamed") as ship:
            attempt = self._record({"summary": "shipped", "commands_executed": ["t3 pr create 1"]}, "claude_sdk")

        assert attempt.error == ""
        ship.assert_not_called()


class TestTheWorkerShippingBriefIsUnchanged(TestCase):
    def test_a_worker_shipping_run_is_taught_and_held_to_commands_executed_alone(self) -> None:
        taught = "\n".join(envelope_contract_lines("shipping"))

        assert required_evidence_for_phase("shipping") == ("commands_executed",)
        assert "ALSO requires a non-empty `commands_executed`" in taught
        assert "ship_readiness" not in taught

    def test_the_executor_shipping_brief_teaches_the_verdict_it_must_return(self) -> None:
        taught = "\n".join(envelope_contract_lines("shipping", read_only_venue=True))

        assert "ALSO requires a non-empty `ship_readiness`" in taught
        assert "ALSO requires a non-empty `commands_executed`" not in taught
