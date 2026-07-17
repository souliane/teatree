# test-path: cross-cutting
"""``t3 worker`` group — bare-run alias + status/ensure controls (#1796 / PR-28).

``status`` reports the live flock holder + the resolved kill-switch tier + timer
counts; ``ensure`` spawns a detached worker iff enabled AND the flock is free, and
refuses (with the reason) otherwise. The DB-touching paths run under a real test DB.
"""

import json
from unittest import mock

import django.test
from typer.testing import CliRunner

import teatree.cli.worker as worker_cli
from teatree.cli.doctor.checks_runtime import _check_worker_running
from teatree.cli.worker import worker_app

runner = CliRunner()


class TestWorkerStatus(django.test.TestCase):
    def test_status_reports_not_running_and_enabled_by_default(self) -> None:
        with mock.patch.object(worker_cli, "_flock_holder_pid", return_value=None):
            result = runner.invoke(worker_app, ["status"])
        assert result.exit_code == 0
        assert "NOT running" in result.stdout
        # PR-28 default is ON, so a not-running worker is surfaced as actionable.
        assert "loop_runner_enabled: True" in result.stdout
        assert "t3 worker ensure" in result.stdout

    def test_status_json_shape(self) -> None:
        with mock.patch.object(worker_cli, "_flock_holder_pid", return_value=4242):
            result = runner.invoke(worker_app, ["status", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["running"] is True
        assert payload["holder_pid"] == 4242
        assert payload["loop_runner_enabled"] is True
        assert payload["source"] == "default"
        assert isinstance(payload["timers"], dict)


class TestWorkerEnsure(django.test.TestCase):
    def test_ensure_refuses_when_kill_switch_off(self) -> None:
        with mock.patch.object(worker_cli, "_resolve_kill_switch", return_value=(False, "global")):
            result = runner.invoke(worker_app, ["ensure"])
        assert result.exit_code == 1
        assert "disabled" in result.stdout

    def test_ensure_reports_already_running(self) -> None:
        with (
            mock.patch.object(worker_cli, "_resolve_kill_switch", return_value=(True, "default")),
            mock.patch("teatree.utils.singleton.flock_is_held", return_value=True),
        ):
            result = runner.invoke(worker_app, ["ensure"])
        assert result.exit_code == 0
        assert "already-running" in result.stdout

    def test_ensure_spawns_when_enabled_and_flock_free(self) -> None:
        spawns: list[bool] = []
        with (
            mock.patch.object(worker_cli, "_resolve_kill_switch", return_value=(True, "default")),
            mock.patch("teatree.utils.singleton.flock_is_held", return_value=False),
            mock.patch(
                "teatree.utils.worker_spawn.spawn_detached_worker", side_effect=lambda: spawns.append(True) or True
            ),
        ):
            result = runner.invoke(worker_app, ["ensure", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["action"] == "spawned"
        assert spawns == [True]

    def test_ensure_errors_when_t3_absent(self) -> None:
        with (
            mock.patch.object(worker_cli, "_resolve_kill_switch", return_value=(True, "default")),
            mock.patch("teatree.utils.singleton.flock_is_held", return_value=False),
            mock.patch("teatree.utils.worker_spawn.spawn_detached_worker", return_value=False),
        ):
            result = runner.invoke(worker_app, ["ensure"])
        assert result.exit_code == 1
        assert "error" in result.stdout


class TestDoctorWorkerCheck(django.test.TestCase):
    """The `t3 doctor` warn: enabled worker + free flock ⇒ actionable ensure nudge (PR-28)."""

    def test_warns_when_enabled_but_flock_free(self) -> None:
        echoed: list[str] = []
        with (
            mock.patch("teatree.utils.singleton.flock_is_held", return_value=False),
            mock.patch("teatree.cli.doctor.checks_runtime.typer.echo", side_effect=echoed.append),
        ):
            assert _check_worker_running() is True  # a WARN, never a hard FAIL
        assert any("t3 worker ensure" in line for line in echoed)

    def test_silent_when_a_worker_holds_the_flock(self) -> None:
        echoed: list[str] = []
        with (
            mock.patch("teatree.utils.singleton.flock_is_held", return_value=True),
            mock.patch("teatree.cli.doctor.checks_runtime.typer.echo", side_effect=echoed.append),
        ):
            assert _check_worker_running() is True
        assert echoed == []
