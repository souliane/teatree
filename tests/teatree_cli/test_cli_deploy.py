"""``t3 deploy roll`` — rolls the stack to an image generation and never falls silent while it waits."""

import os
import signal
import sys
import tempfile
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import django.test
import pytest
from click.testing import Result
from django.core.management import call_command
from typer.testing import CliRunner

from teatree.cli.deploy import deploy_app
from teatree.core.management.commands.deploy_roll import RollProgress
from teatree.core.models import WorkerGeneration
from teatree.deploy.roll import RUNTIME_SERVICES
from teatree.generation import generation_image
from tests.teatree_deploy._fake_engine import FakeEngine

runner = CliRunner()
N = "a" * 40
N1 = "b" * 40


def _roll(engine: FakeEngine, *args: str, record: str | None = None, roll_pid: str | None = None) -> Result:
    now = int(time.time())
    with tempfile.TemporaryDirectory() as scratch:
        lock = Path(scratch) / "teatree-deploy.lock"
        lock.write_text(f"{os.getpid()} {now} {now + 3600}\n" if record is None else record, encoding="utf-8")
        environ = {"TEATREE_DEPLOY_LOCK": str(lock), "TEATREE_ROLL_RECORD_PID": roll_pid or str(os.getpid())}
        with (
            patch.dict("os.environ", environ),
            patch("teatree.deploy.compose_engine.DockerComposeEngine.from_environment", return_value=engine),
        ):
            argv = ["roll", "--to", N1, "--verify-timeout", "0", "--stable-seconds", "0", *args]
            return runner.invoke(deploy_app, argv)


def _engine() -> FakeEngine:
    return FakeEngine(images={generation_image(N1): N1}, running=dict.fromkeys(RUNTIME_SERVICES, N))


class TestDeployRoll(django.test.TestCase):
    def setUp(self) -> None:
        WorkerGeneration.objects.boot(N)

    def test_a_roll_exits_zero_and_names_both_generations(self) -> None:
        result = _roll(_engine())

        assert result.exit_code == 0, result.output
        assert "rolled aaaaaaaaaaaa -> bbbbbbbbbbbb" in result.stdout
        assert "roll: drain" in result.stderr

    def test_the_management_command_runs_the_roll_through_call_command(self) -> None:
        out, err = StringIO(), StringIO()
        with patch("teatree.deploy.compose_engine.DockerComposeEngine.from_environment", return_value=_engine()):
            call_command("deploy_roll", to=N1, verify_timeout=0, stable_seconds=0, stdout=out, stderr=err)

        assert "rolled aaaaaaaaaaaa -> bbbbbbbbbbbb" in out.getvalue()
        assert "roll: promote" in err.getvalue()
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.ACTIVE

    def test_a_rolled_back_roll_exits_non_zero_and_says_the_old_generation_serves(self) -> None:
        engine = _engine()
        engine.fail_init = True

        result = _roll(engine)

        assert result.exit_code == 3
        assert "rolled back" in result.output
        assert "aaaaaaaaaaaa is serving" in result.output
        assert "teatree-init exited 1" in result.output

    def test_a_refused_roll_exits_one_with_the_reason(self) -> None:
        engine = FakeEngine(images={}, running=dict.fromkeys(RUNTIME_SERVICES, N))

        result = _roll(engine)

        assert result.exit_code == 1
        assert "is not built" in result.output

    def test_a_sigterm_mid_roll_restores_the_old_generation_and_says_so(self) -> None:
        engine = _engine()
        engine.signal_init = signal.SIGTERM

        result = _roll(engine)

        assert result.exit_code == 3, result.output
        assert "interrupted by SIGTERM" in result.output
        assert "aaaaaaaaaaaa is serving" in result.output
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE

    def test_an_interrupt_during_preflight_says_nothing_moved(self) -> None:
        engine = _engine()
        engine.interrupt_preflight = True

        result = _roll(engine)

        assert result.exit_code == 1, result.output
        assert "interrupted by SIGTERM before anything moved" in result.output
        assert "nothing is known to serve" not in result.output
        assert WorkerGeneration.objects.state_of(N) == WorkerGeneration.State.ACTIVE

    def test_an_interrupt_after_verification_says_the_new_generation_serves_unpromoted(self) -> None:
        engine = _engine()
        engine.interrupt_promote = True

        result = _roll(engine)

        assert result.exit_code == 1, result.output
        assert "rolled back" not in result.output
        assert "after bbbbbbbbbbbb verified — it serves" in result.output
        assert WorkerGeneration.objects.state_of(N1) == WorkerGeneration.State.ACTIVE

    def test_a_roll_that_cannot_roll_back_across_the_schema_exits_one(self) -> None:
        engine = _engine()
        engine.init_applies = "9999_zdd_cli_probe"
        engine.fail_init = True

        result = _roll(engine)

        assert result.exit_code == 1
        assert "refusing to roll back" in result.output

    def test_a_crash_looping_listener_is_rolled_back_unless_declared_optional(self) -> None:
        looping = _engine()
        looping.crash_looping = {"teatree-slack-listener"}
        looping.crash_loop_of = N1

        refused = _roll(looping, "--stable-seconds", "1", "--verify-timeout", "6")

        assert refused.exit_code == 3, refused.output
        assert "teatree-slack-listener has not stayed up" in refused.output

        declared = _roll(looping, "--optional-service", "teatree-slack-listener")

        assert declared.exit_code == 0, declared.output

    def test_declaring_the_worker_optional_is_refused(self) -> None:
        result = _roll(_engine(), "--optional-service", "teatree-worker")

        assert result.exit_code == 1
        assert "cannot be optional" in result.output

    def test_rolling_to_the_serving_generation_is_reported_as_such(self) -> None:
        engine = _engine()
        _roll(engine)

        result = _roll(engine)

        assert result.exit_code == 0
        assert "already serving bbbbbbbbbbbb" in result.stdout


class TestOnlyADeployHoldingTheLockRolls(django.test.TestCase):
    def setUp(self) -> None:
        WorkerGeneration.objects.boot(N)

    def test_a_roll_no_deploy_recorded_is_refused_before_it_touches_the_stack(self) -> None:
        engine = _engine()

        result = _roll(engine, record="")

        assert result.exit_code == 1
        assert "deploy/roll.sh" in result.output
        assert engine.calls == []

    def test_a_beating_record_another_deploy_keeps_is_refused(self) -> None:
        engine = _engine()

        result = _roll(engine, roll_pid="4242")

        assert result.exit_code == 1
        assert "not held by the deploy/roll.sh that started this roll" in result.output
        assert engine.calls == []

    def test_a_record_whose_heartbeat_stopped_is_refused(self) -> None:
        engine = _engine()
        stale = int(time.time()) - 3600

        result = _roll(engine, record=f"4242 {stale} {stale + 7200}\n", roll_pid="4242")

        assert result.exit_code == 1
        assert engine.calls == []


class TestRollProgress:
    def test_the_current_step_is_repeated_while_it_runs(self, capsys: pytest.CaptureFixture[str]) -> None:
        with RollProgress(output=sys.stderr, interval=0.01) as progress:
            progress("drain")
            time.sleep(0.1)

        lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("roll: drain")]
        assert len(lines) >= 3

    def test_the_default_cadence_stays_inside_a_minute(self) -> None:
        assert RollProgress(output=StringIO()).interval <= 60
