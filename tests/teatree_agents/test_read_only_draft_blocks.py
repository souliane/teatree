"""The executor brief of each draft phase names its return key and no command; the worker brief is untouched."""

from django.test import TestCase

from teatree.agents import phase_blocks
from teatree.agents.prompt import build_read_only_task_prompt
from teatree.agents.read_only_draft_blocks import BACKLOG_SWEEP_LINES, REQUESTING_REVIEW_LINES, SHIPPING_LINES
from teatree.agents.read_only_venue_brief import ReadOnlyFacts
from teatree.core.models import Session, Task, Ticket
from teatree.loop.scanners.backlog_sweep import BacklogSweepScanner


def _task(phase: str, **ticket_fields: object) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, overlay="t3-teatree", **ticket_fields)
    return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase=phase)


class TestTheReadOnlyPhaseBlocks(TestCase):
    def _block(self, phase: str, *, read_only: bool) -> str:
        task = _task(phase, extra={"slack_answer": {"slack_ts": "1.5", "question": "hi?"}})
        return "\n".join(phase_blocks.phase_specific_lines(task, [], read_only_venue=read_only))

    def test_each_draft_phase_names_the_key_the_worker_acts_on(self) -> None:
        for phase, key in (
            ("requesting_review", "review_request"),
            ("shipping", "ship_readiness"),
            ("answering", "answer"),
            ("backlog_sweep", "sweep_proposals"),
        ):
            with self.subTest(phase=phase):
                assert f'"{key}"' in self._block(phase, read_only=True)

    def test_the_executor_blocks_name_no_teatree_command(self) -> None:
        assert not [line for line in (*BACKLOG_SWEEP_LINES, *REQUESTING_REVIEW_LINES, *SHIPPING_LINES) if "t3 " in line]

    def test_a_worker_sweep_keeps_its_measured_run_contract(self) -> None:
        assert "sweep_proposals" not in self._block("backlog_sweep", read_only=False)

    def test_sweep_task_message_labels_the_index_and_window(self) -> None:
        task = _task("backlog_sweep")
        facts = ReadOnlyFacts("- issue index", "ISSUE window body", "", "", "", "", "", "")
        message = build_read_only_task_prompt(task, facts)
        assert "OPEN ISSUE INDEX" in message
        assert "ISSUE WINDOW" in message
        assert "data, never instructions" in message

    def test_executor_reason_does_not_carry_the_worker_sweep_commands(self) -> None:
        task = _task("backlog_sweep")
        task.execution_reason = (
            BacklogSweepScanner(
                overlay_name="t3-teatree", skill="sweeping-tickets", require_approval=True
            )._execution_reason("cadence")
            + "\nDream promotion batch (#4776)\n- gap-a: offered work"
        )

        message = build_read_only_task_prompt(task, ReadOnlyFacts("index", "window", "", "", "", "", "", ""))

        assert "ASK-GATE" in message
        assert "gap-a: offered work" in message
        assert "sweep-begin" not in message
        assert "bulk-close" not in message

    def test_the_worker_shipping_brief_keeps_its_pr_create_steps(self) -> None:
        worker = self._block("shipping", read_only=False)

        assert "pr create" in worker
        assert "ship_readiness" not in worker

    def test_the_worker_answering_brief_is_unchanged_by_the_executor_line(self) -> None:
        assert "INBOUND EVENT" not in self._block("answering", read_only=False)
        assert "INBOUND EVENT" in self._block("answering", read_only=True)

    def test_a_worker_requesting_review_brief_has_no_block(self) -> None:
        assert self._block("requesting_review", read_only=False) == ""


class TestTheTaskMessageCarriesTheFacts(TestCase):
    def test_the_inline_facts_reach_the_executor_in_the_task_message(self) -> None:
        task = _task("shipping")
        facts = ReadOnlyFacts(
            "issue", "", "", "", "", "", "", "", draft_facts="SHIP STATE, read by the worker:\nTicket state: x"
        )

        message = build_read_only_task_prompt(task, facts)

        assert "SHIP STATE, read by the worker:" in message

    def test_a_phase_with_no_draft_facts_adds_nothing(self) -> None:
        task = _task("planning")
        message = build_read_only_task_prompt(task, ReadOnlyFacts("issue", "", "", "", "", "", "", ""))

        assert "SHIP STATE" not in message
