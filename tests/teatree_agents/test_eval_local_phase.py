"""The scheduled local eval must run the real CLI lane and record a measurable result."""

import os
from unittest.mock import patch

from django.test import TestCase

from teatree.core.deterministic_phases import deterministic_phase_runner, run_deterministic_phase
from teatree.core.models import EvalRunRecord, EvalVerdict, Loop, Session, Task, Ticket


class TestEvalLocalPhase(TestCase):
    def _task(self) -> Task:
        ticket = Ticket.objects.create(overlay="t3-teatree", extra={"issue_title": "local eval"})
        session = Session.objects.create(ticket=ticket, agent_id="eval-local")
        return Task.objects.create(ticket=ticket, session=session, phase="eval_local")

    def test_runner_is_registered(self) -> None:
        assert deterministic_phase_runner("eval_local") is not None

    def test_a_measured_run_completes_and_never_passes_the_metered_key(self) -> None:
        task = self._task()
        Loop.objects.filter(name="eval_local").update(delay_seconds=86400)
        commands: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> object:
            commands.append(argv)
            assert "ANTHROPIC_API_KEY" not in kwargs["env"]
            output = "alpha\n" if "list" in argv else ""
            return type("Completed", (), {"returncode": 0, "stdout": output, "stderr": ""})()

        class FakeChild:
            returncode = 1

            def communicate(self, *, timeout: float) -> tuple[str, str]:
                run = EvalRunRecord.objects.create(model="claude-sonnet", suite="local")
                run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.FAIL, cost_usd=0.42)
                return "", ""

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "metered-secret"}),
            patch("teatree.agents.eval_local_phase.run_allowed_to_fail", side_effect=fake_run),
            patch("teatree.agents.eval_local_phase.spawn_session_leader", return_value=FakeChild()) as spawn,
        ):
            result = run_deterministic_phase(task)

        assert result is not None
        assert result["exit_code"] == "0"
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert any(argv[-3:] == ["--names-only", "--shard", "rotate/7"] for argv in commands)
        run_argv = spawn.call_args.args[0]
        assert run_argv[5:9] == ["--backend", "api", "--credential", "subscription_oauth"]
        assert "--local" in run_argv
        assert "--persist" in run_argv
        assert "rotate/7" in run_argv
        assert "ANTHROPIC_API_KEY" not in spawn.call_args.kwargs["env"]
        assert EvalRunRecord.objects.latest("pk").failed == 1

    def test_unavailable_lane_records_all_skips_and_fails_the_task(self) -> None:
        task = self._task()

        def fake_run(argv: list[str], **kwargs: object) -> object:
            return type("Completed", (), {"returncode": 0, "stdout": "alpha\nbeta\n", "stderr": ""})()

        class FakeChild:
            returncode = 2

            def communicate(self, *, timeout: float) -> tuple[str, str]:
                return "", "lane unavailable"

        with (
            patch("teatree.agents.eval_local_phase.run_allowed_to_fail", side_effect=fake_run),
            patch("teatree.agents.eval_local_phase.spawn_session_leader", return_value=FakeChild()),
        ):
            result = run_deterministic_phase(task)

        assert result is not None
        assert result["exit_code"] == "1"
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "measured nothing" in task.attempts.latest("pk").error
        run = EvalRunRecord.objects.latest("pk")
        assert run.total == 2
        assert run.skipped == 2
        assert run.measured_nothing
