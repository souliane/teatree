# test-path: cross-cutting
"""The scheduled local eval must run the real CLI lane and record a measurable result."""

import os
from pathlib import Path
from subprocess import TimeoutExpired
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pytest
from django.test import TestCase
from typer.testing import CliRunner

from teatree.agents.eval_local_phase import _command, _run_child
from teatree.cli import app
from teatree.core.deterministic_phases import run_deterministic_phase
from teatree.core.models import EvalRunRecord, EvalVerdict, Loop, Session, Task, Ticket
from teatree.eval.models import EvalSpec, Matcher
from teatree.utils.run import run_allowed_to_fail


class TestEvalLocalPhase(TestCase):
    def _task(self) -> Task:
        ticket = Ticket.objects.create(overlay="t3-teatree", extra={"issue_title": "local eval"})
        session = Session.objects.create(ticket=ticket, agent_id="eval-local")
        return Task.objects.create(ticket=ticket, session=session, phase="eval_local")

    def test_catalog_command_uses_the_real_t3_cli(self) -> None:
        result = run_allowed_to_fail([*_command("list"), "--help"], timeout=30)
        assert result.returncode == 0
        assert "--names-only" in result.stdout

    def test_child_renews_lease_after_a_heartbeat_timeout(self) -> None:
        task = Mock(spec=Task)
        child = Mock(pid=123, returncode=0)
        child.communicate.side_effect = [TimeoutExpired("eval", 60), ("done", "")]
        with patch("teatree.agents.eval_local_phase.spawn_session_leader", return_value=child):
            _run_child(task, "rotate/7", ["alpha"], {})
        task.renew_lease.assert_called_once()

    def test_child_group_is_killed_at_wall_clock_deadline(self) -> None:
        task = Mock(spec=Task)
        child = Mock(pid=123, returncode=None)
        child.communicate.side_effect = [TimeoutExpired("eval", 60), ("", "deadline")]
        with (
            patch("teatree.agents.eval_local_phase._SECONDS_PER_SCENARIO", 0),
            patch("teatree.agents.eval_local_phase.spawn_session_leader", return_value=child),
            patch("teatree.agents.eval_local_phase.os.killpg") as killpg,
        ):
            result = _run_child(task, "rotate/7", ["alpha"], {})
        assert result[0] == 124
        killpg.assert_called_once()

    def test_lease_loss_kills_the_detached_child_group(self) -> None:
        task = Mock(spec=Task)
        task.renew_lease.side_effect = RuntimeError("lease lost")
        child = Mock(pid=123, returncode=None)
        child.communicate.side_effect = [TimeoutExpired("eval", 60), ("", "")]
        with (
            patch("teatree.agents.eval_local_phase.spawn_session_leader", return_value=child),
            patch("teatree.agents.eval_local_phase.os.killpg") as killpg,
            pytest.raises(RuntimeError, match="lease lost"),
        ):
            _run_child(task, "rotate/7", ["alpha"], {})
        killpg.assert_called_once()
        assert child.communicate.call_count == 2

    def test_a_measured_run_completes_and_never_passes_the_metered_key(self) -> None:
        task = self._task()
        Loop.objects.filter(name="eval_local").update(delay_seconds=86400)
        commands: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> object:
            commands.append(argv)
            assert "ANTHROPIC_API_KEY" not in kwargs["env"]
            assert kwargs["timeout"] == 30
            output = "alpha\n" if "list" in argv else ""
            return type("Completed", (), {"returncode": 0, "stdout": output, "stderr": ""})()

        class FakeChild:
            returncode = 1

            def communicate(self, *, timeout: float) -> tuple[str, str]:
                run = EvalRunRecord.objects.create(model="claude-sonnet", suite=f"eval_local:{task.pk}")
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
        assert run_argv[run_argv.index("--backend") : run_argv.index("--backend") + 4] == [
            "--backend",
            "api",
            "--credential",
            "subscription_oauth",
        ]
        assert "--local" in run_argv
        assert "--persist" in run_argv
        assert "rotate/7" in run_argv
        assert "ANTHROPIC_API_KEY" not in spawn.call_args.kwargs["env"]
        assert spawn.call_args.kwargs["env"][EvalRunRecord.RECORD_SUITE_ENV] == f"eval_local:{task.pk}"
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
        assert "lane unavailable" in task.attempts.latest("pk").error
        assert "lane unavailable" in run.results.first().terminal_reason

    def test_unrelated_run_cannot_make_a_failed_lane_measured(self) -> None:
        task = self._task()

        def fake_run(argv: list[str], **kwargs: object) -> object:
            output = "alpha\n" if "list" in argv else ""
            return type("Completed", (), {"returncode": 0, "stdout": output, "stderr": ""})()

        class FakeChild:
            returncode = 2

            def communicate(self, *, timeout: float) -> tuple[str, str]:
                unrelated = EvalRunRecord.objects.create(model="manual", suite="other")
                unrelated.record_scenario(scenario_name="alpha", verdict=EvalVerdict.PASS)
                return "", ""

        with (
            patch("teatree.agents.eval_local_phase.run_allowed_to_fail", side_effect=fake_run),
            patch("teatree.agents.eval_local_phase.spawn_session_leader", return_value=FakeChild()),
        ):
            result = run_deterministic_phase(task)

        assert result is not None
        assert result["exit_code"] == "1"
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        local = EvalRunRecord.objects.get(suite=f"eval_local:{task.pk}")
        assert local.measured_nothing

    def test_unavailable_catalog_records_an_empty_failed_run(self) -> None:
        task = self._task()
        failure = type("Completed", (), {"returncode": 2, "stdout": "", "stderr": "catalog unavailable"})()

        with patch("teatree.agents.eval_local_phase.run_allowed_to_fail", return_value=failure):
            result = run_deterministic_phase(task)

        assert result is not None
        assert result["exit_code"] == "1"
        run = EvalRunRecord.objects.latest("pk")
        assert run.total == 0
        assert not run.measured_nothing
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_empty_catalog_keeps_its_distinct_failure_reason(self) -> None:
        task = self._task()
        empty = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch("teatree.agents.eval_local_phase.run_allowed_to_fail", return_value=empty):
            result = run_deterministic_phase(task)
        assert result is not None
        assert result["exit_code"] == "1"
        assert "selected scenario shard is empty" in task.attempts.latest("pk").error

    def test_pinned_timeout_records_the_selected_shard_as_unmeasured(self) -> None:
        task = self._task()

        def fake_run(argv: list[str], **_: object) -> object:
            if "pinned-regressions" in argv:
                raise TimeoutExpired(argv, 30)
            return type("Completed", (), {"returncode": 0, "stdout": "alpha\n", "stderr": ""})()

        with patch("teatree.agents.eval_local_phase.run_allowed_to_fail", side_effect=fake_run):
            result = run_deterministic_phase(task)

        assert result is not None
        assert result["exit_code"] == "1"
        run = EvalRunRecord.objects.get(suite=f"eval_local:{task.pk}")
        assert run.total == 1
        assert run.measured_nothing
        assert "pinned-regressions" in run.results.first().terminal_reason

    def test_pinned_regressions_run_strict_and_fail_a_measured_task(self) -> None:
        task = self._task()
        commands: list[list[str]] = []

        def fake_run(argv: list[str], **_: object) -> object:
            commands.append(argv)
            output = "alpha\n" if "list" in argv else ""
            code = 1 if "pinned-regressions" in argv else 0
            return type("Completed", (), {"returncode": code, "stdout": output, "stderr": "regression failed"})()

        class FakeChild:
            returncode = 0

            def communicate(self, *, timeout: float) -> tuple[str, str]:
                run = EvalRunRecord.objects.create(model="subscription", suite=f"eval_local:{task.pk}")
                run.record_scenario(scenario_name="alpha", verdict=EvalVerdict.PASS)
                return "", ""

        with (
            patch("teatree.agents.eval_local_phase.run_allowed_to_fail", side_effect=fake_run),
            patch("teatree.agents.eval_local_phase.spawn_session_leader", return_value=FakeChild()),
        ):
            result = run_deterministic_phase(task)
        assert result is not None
        assert result["exit_code"] == "1"
        assert "--strict" in next(argv for argv in commands if "pinned-regressions" in argv)
        assert "regression failed" in task.attempts.latest("pk").error

    def test_loop_entry_records_every_selected_transcript_scenario(self) -> None:
        task = self._task()
        names = ("alpha", "beta")
        specs = [
            EvalSpec(
                name=name,
                scenario=f"scenario for {name}",
                agent_path="skills/code/SKILL.md",
                prompt="do",
                matchers=(
                    Matcher(
                        kind="positive", tool="Bash", arg_path="command", operator="contains", value="git worktree add"
                    ),
                ),
                source_path=Path("/tmp/spec.yaml"),
            )
            for name in names
        ]
        fixture = Path(__file__).resolve().parents[2] / "evals" / "fixtures" / "worktree_first_pass.stream.jsonl"

        def fake_run(argv: list[str], **_: object) -> object:
            output = "alpha\nbeta\n" if "list" in argv else ""
            return type("Completed", (), {"returncode": 0, "stdout": output, "stderr": ""})()

        with TemporaryDirectory() as directory:
            for name in names:
                (Path(directory) / f"{name}.jsonl").write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")

            class TranscriptChild:
                returncode = 0

                def communicate(self, *, timeout: float) -> tuple[str, str]:
                    with (
                        patch("teatree.cli.eval.app.discover_specs", return_value=specs),
                        patch.dict(os.environ, {EvalRunRecord.RECORD_SUITE_ENV: f"eval_local:{task.pk}"}),
                    ):
                        result = CliRunner().invoke(
                            app,
                            ["eval", "run", "--backend", "transcript", "--transcript-dir", directory, "--persist"],
                        )
                    self.returncode = result.exit_code
                    return result.output, ""

            with (
                patch("teatree.agents.eval_local_phase.run_allowed_to_fail", side_effect=fake_run),
                patch("teatree.agents.eval_local_phase.spawn_session_leader", return_value=TranscriptChild()),
            ):
                result = run_deterministic_phase(task)

        assert result is not None
        assert result["exit_code"] == "0"
        run = EvalRunRecord.objects.latest("pk")
        assert run.executed == 2
        assert sorted(run.results.values_list("scenario_name", flat=True)) == list(names)
        assert run.passed == 2
