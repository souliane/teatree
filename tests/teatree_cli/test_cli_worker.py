# test-path: cross-cutting
"""``t3 worker`` group — bare-run alias + status/ensure/drain/stop/restart controls (#1796).

``status`` reports the live flock holder + the resolved kill-switch tier + timer
counts; ``ensure`` spawns a detached worker iff enabled AND the flock is free, and
refuses (with the reason) otherwise; ``stop`` drains then signals the flock holder and
verifies the exit; ``restart`` proves a NEW holder took the flock. The DB-touching
paths run under a real test DB; no test signals a real process.
"""

import datetime as dt
import json
import threading
from pathlib import Path
from unittest import mock

import django.test
import pytest
from django.utils import timezone
from typer.testing import CliRunner

import teatree.cli.worker as worker_cli
from teatree.cli.doctor.checks_runtime import _check_singletons, _check_worker_running
from teatree.cli.worker import DrainPayload, _drain_payload, worker_app
from teatree.core.models import ConfigSetting, Loop, Prompt
from teatree.loop.drain import QUIESCING_SETTING, DrainOutcome, DrainReport, set_worker_quiescing
from teatree.loop.worker_lifecycle import StartReport, StopOutcome, StopReport, WorkerStopper
from teatree.loops.loop_staleness import Admission, LoopHealth
from teatree.utils import singleton as singleton_mod

runner = CliRunner()


def _healthy_loop_health() -> LoopHealth:
    """A green loop-fleet reading, stubbed so ``status`` exercises its OWN exit logic.

    ``status_command`` folds ``loop_health(now)`` into its exit code, and that read
    reflects ambient fleet health in the shared DB — fresh locally, stale on a
    populated CI shard. Pinning a healthy reading here keeps each running-state test
    asserting the flock/pid-file logic under test, not the shard's loop staleness.
    """
    return LoopHealth(
        admission=Admission(mode="standard", source="default", admitted=("tickets",), enabled_total=1),
        stale=(),
        considered=1,
    )


class TestWorkerStatus(django.test.TestCase):
    def test_status_reports_not_running_and_enabled_by_default(self) -> None:
        with (
            mock.patch.object(worker_cli, "_flock_holder_pid", return_value=None),
            mock.patch("teatree.utils.singleton.flock_is_held", return_value=False),
            mock.patch("teatree.loops.loop_staleness.loop_health", return_value=_healthy_loop_health()),
        ):
            result = runner.invoke(worker_app, ["status"])
        assert result.exit_code == 0
        assert "NOT running" in result.stdout
        # PR-28 default is ON, so a not-running worker is surfaced as actionable.
        assert "loop_runner_enabled: True" in result.stdout
        assert "t3 worker ensure" in result.stdout

    def test_status_json_shape(self) -> None:
        with (
            mock.patch.object(worker_cli, "_flock_holder_pid", return_value=4242),
            mock.patch("teatree.loops.loop_staleness.loop_health", return_value=_healthy_loop_health()),
        ):
            result = runner.invoke(worker_app, ["status", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["running"] is True
        assert payload["holder_pid"] == 4242
        assert payload["loop_runner_enabled"] is True
        assert payload["source"] == "default"
        assert isinstance(payload["timers"], dict)

    def test_status_reports_running_via_flock_when_pid_file_absent(self) -> None:
        # The flock is HELD by a live worker but the pid file is missing/stale, so
        # `read_pid` returns None — status must not print a false "NOT running" (#3571).
        with (
            mock.patch.object(worker_cli, "_flock_holder_pid", return_value=None),
            mock.patch("teatree.utils.singleton.flock_is_held", return_value=True),
            mock.patch("teatree.loops.loop_staleness.loop_health", return_value=_healthy_loop_health()),
        ):
            result = runner.invoke(worker_app, ["status"])
        assert result.exit_code == 0
        assert "NOT running" not in result.stdout
        assert "RUNNING" in result.stdout
        assert "t3 worker ensure" not in result.stdout

    def test_status_json_flock_fallback_marks_running(self) -> None:
        with (
            mock.patch.object(worker_cli, "_flock_holder_pid", return_value=None),
            mock.patch("teatree.utils.singleton.flock_is_held", return_value=True),
            mock.patch("teatree.loops.loop_staleness.loop_health", return_value=_healthy_loop_health()),
        ):
            result = runner.invoke(worker_app, ["status", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["running"] is True
        assert payload["holder_pid"] is None
        assert payload["flock_held"] is True

    def test_status_not_running_when_flock_free(self) -> None:
        with (
            mock.patch.object(worker_cli, "_flock_holder_pid", return_value=None),
            mock.patch("teatree.utils.singleton.flock_is_held", return_value=False),
            mock.patch("teatree.loops.loop_staleness.loop_health", return_value=_healthy_loop_health()),
        ):
            result = runner.invoke(worker_app, ["status"])
        assert result.exit_code == 0
        assert "NOT running" in result.stdout

    def test_status_reports_the_resolved_mode_and_admitted_count(self) -> None:
        with (
            mock.patch.object(worker_cli, "_flock_holder_pid", return_value=4242),
            mock.patch("teatree.loops.loop_staleness.loop_health", return_value=_healthy_loop_health()),
        ):
            result = runner.invoke(worker_app, ["status"])
        assert result.exit_code == 0
        assert "enabled loop(s) admitted" in result.stdout

    @staticmethod
    def _frozen_loop() -> None:
        """One enabled loop whose anchor stopped seven hours ago, on an otherwise empty table."""
        Loop.objects.all().delete()
        prompt, _ = Prompt.objects.get_or_create(name="demo-prompt", defaults={"body": "do x"})
        Loop.objects.create(
            name="tickets",
            prompt=prompt,
            delay_seconds=300,
            last_run_at=timezone.now() - dt.timedelta(hours=7),
        )

    def test_status_fails_when_a_live_worker_ticks_nothing(self) -> None:
        # The seven-hour silent freeze: flock held, kill-switch ON, timers READY —
        # and a mode mask that admits no loop, so no anchor moves. Status must FAIL.
        self._frozen_loop()
        with (
            mock.patch.object(worker_cli, "_flock_holder_pid", return_value=4242),
            mock.patch("teatree.loops.loop_table.admitted_loop_names", return_value=[]),
        ):
            result = runner.invoke(worker_app, ["status"])
        assert result.exit_code == 1
        assert "RUNNING (pid 4242)" in result.stdout
        assert "loop_runner_enabled: True" in result.stdout
        assert "ticking NOTHING" in result.stdout
        assert "tickets" in result.stdout

    def test_status_json_fails_and_carries_the_stale_set(self) -> None:
        self._frozen_loop()
        with (
            mock.patch.object(worker_cli, "_flock_holder_pid", return_value=4242),
            mock.patch("teatree.loops.loop_table.admitted_loop_names", return_value=[]),
        ):
            result = runner.invoke(worker_app, ["status", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        # `deploy.sh` greps this key out of the body, so a failing exit must not
        # cost it the running signal it converges on.
        assert payload["running"] is True
        assert payload["admitted"] == []
        assert [entry["name"] for entry in payload["stale"]] == ["tickets"]


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
            mock.patch(
                "teatree.loop.worker_lifecycle.wait_for_new_holder",
                return_value=StartReport(started=True, holder_pid=999, waited_seconds=2.0),
            ),
        ):
            result = runner.invoke(worker_app, ["ensure", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["action"] == "spawned"
        assert spawns == [True]

    def test_ensure_reports_a_spawn_that_never_took_the_flock(self) -> None:
        # The defect this closes: the spawner returns True as soon as the `t3` binary
        # exists and the child's streams go to DEVNULL, so a startup crash read as
        # "spawned". The verdict now rests on the flock, and the child's output is shown.
        with (
            mock.patch.object(worker_cli, "_resolve_kill_switch", return_value=(True, "default")),
            mock.patch("teatree.utils.singleton.flock_is_held", return_value=False),
            mock.patch("teatree.utils.worker_spawn.spawn_detached_worker", return_value=True),
            mock.patch(
                "teatree.loop.worker_lifecycle.wait_for_new_holder",
                return_value=StartReport(started=False, holder_pid=None, waited_seconds=60.0),
            ),
            mock.patch(
                "teatree.utils.worker_spawn.read_spawn_log_tail",
                return_value="ModuleNotFoundError: No module named 'teatree'",
            ),
        ):
            result = runner.invoke(worker_app, ["ensure"])
        assert result.exit_code == 1
        assert "unverified" in result.stdout
        assert "ModuleNotFoundError" in result.stdout
        assert "t3 worker status" in result.stdout

    def test_ensure_errors_when_t3_absent(self) -> None:
        with (
            mock.patch.object(worker_cli, "_resolve_kill_switch", return_value=(True, "default")),
            mock.patch("teatree.utils.singleton.flock_is_held", return_value=False),
            mock.patch("teatree.utils.worker_spawn.spawn_detached_worker", return_value=False),
        ):
            result = runner.invoke(worker_app, ["ensure"])
        assert result.exit_code == 1
        assert "error" in result.stdout


def test_ensure_is_a_no_op_for_a_live_flock_holder_with_a_stale_pid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The end-to-end #3617 bug: a live worker holds the flock, its lock file records
    # a dead pid, and a prior diagnostic `read_pid` reap (status/doctor) ran. `ensure`
    # must still detect the live holder via the flock and spawn NOTHING.

    monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
    path = singleton_mod.default_pid_path(singleton_mod.WORKER_SINGLETON)

    ready, release = threading.Event(), threading.Event()

    def _hold() -> None:
        with singleton_mod.singleton(singleton_mod.WORKER_SINGLETON, pid_path=path):
            ready.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=_hold)
    holder.start()
    try:
        assert ready.wait(timeout=5), "holder never acquired the flock"
        path.write_text("999999999\n", encoding="utf-8")  # stale pid clobbers the live holder's file
        singleton_mod.read_pid(path)  # a prior status/doctor reap — must NOT orphan the flock

        spawns: list[bool] = []
        with (
            mock.patch.object(worker_cli, "_resolve_kill_switch", return_value=(True, "default")),
            mock.patch(
                "teatree.utils.worker_spawn.spawn_detached_worker",
                side_effect=lambda: spawns.append(True) or True,
            ),
        ):
            result = runner.invoke(worker_app, ["ensure", "--json"])
    finally:
        release.set()
        holder.join(timeout=5)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["action"] == "already-running"
    assert spawns == []


def test_check_singletons_reports_a_stale_idle_lock_file_without_unlinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
    path = singleton_mod.default_pid_path(singleton_mod.WORKER_SINGLETON)
    path.write_text("999999999\n", encoding="utf-8")  # dead pid, no flock held

    echoed: list[str] = []
    with mock.patch("teatree.cli.doctor.checks_runtime.typer.echo", side_effect=echoed.append):
        assert _check_singletons() is True
    assert path.is_file()  # never unlinked
    assert any("stale but idle" in line for line in echoed)


def test_check_singletons_leaves_a_live_flock_holders_file_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    monkeypatch.setattr(singleton_mod, "DATA_DIR", tmp_path)
    path = singleton_mod.default_pid_path(singleton_mod.WORKER_SINGLETON)

    ready, release = threading.Event(), threading.Event()

    def _hold() -> None:
        with singleton_mod.singleton(singleton_mod.WORKER_SINGLETON, pid_path=path):
            ready.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=_hold)
    holder.start()
    try:
        assert ready.wait(timeout=5), "holder never acquired the flock"
        path.write_text("999999999\n", encoding="utf-8")  # stale pid, but the flock IS held
        echoed: list[str] = []
        with mock.patch("teatree.cli.doctor.checks_runtime.typer.echo", side_effect=echoed.append):
            assert _check_singletons() is True
    finally:
        release.set()
        holder.join(timeout=5)

    assert path.is_file()
    assert not any("stale but idle" in line for line in echoed)  # a live holder is never flagged


def _quiescing_drain(report: DrainReport) -> "object":
    """A ``drain_worker`` stand-in that closes the admission gate the way the real one does."""

    def _drain(**_kwargs: object) -> DrainReport:
        set_worker_quiescing(value=True)
        return report

    return _drain


class TestWorkerDrain(django.test.TestCase):
    """`t3 worker drain` — quiesce + wait, exit 0 on drained, 3 on grace-exceeded."""

    def test_drain_payload_maps_a_report_to_the_stop_json_shape(self) -> None:
        report = DrainReport(outcome=DrainOutcome.GRACE_EXCEEDED, waited_seconds=30.0, still_claimed=[7, 9])
        payload: DrainPayload | None = _drain_payload(report)
        assert payload == {"outcome": "grace_exceeded", "still_claimed": [7, 9]}

    def test_drain_payload_is_none_without_a_report(self) -> None:
        assert _drain_payload(None) is None

    def test_drained_exits_zero(self) -> None:
        report = DrainReport(outcome=DrainOutcome.DRAINED, waited_seconds=1.0)
        with mock.patch("teatree.loop.drain.drain_worker", return_value=report):
            result = runner.invoke(worker_app, ["drain", "--timeout", "30"])
        assert result.exit_code == 0
        assert "drained" in result.stdout

    def test_grace_exceeded_exits_distinct_code_and_lists_tasks(self) -> None:
        report = DrainReport(outcome=DrainOutcome.GRACE_EXCEEDED, waited_seconds=30.0, still_claimed=[7, 9])
        with mock.patch("teatree.loop.drain.drain_worker", return_value=report):
            result = runner.invoke(worker_app, ["drain", "--timeout", "30"])
        assert result.exit_code == worker_cli._GRACE_EXCEEDED_EXIT
        assert result.exit_code != 0
        assert "7, 9" in result.stdout

    def test_json_shape(self) -> None:
        report = DrainReport(outcome=DrainOutcome.GRACE_EXCEEDED, waited_seconds=30.5, still_claimed=[7])
        with mock.patch("teatree.loop.drain.drain_worker", side_effect=_quiescing_drain(report)):
            result = runner.invoke(worker_app, ["drain", "--json"])
        payload = json.loads(result.stdout)
        assert payload["outcome"] == "grace_exceeded"
        assert payload["still_claimed"] == [7]
        assert payload["waited_seconds"] == pytest.approx(30.5)
        # Read BACK from the store, not asserted from intent.
        assert payload["worker_quiescing"] is True

    def test_names_what_clears_the_quiesce_it_leaves_behind(self) -> None:
        # The drain leaves the box admitting ZERO work and stops nothing — the recovery
        # must be discoverable from the command's own output, not from the source.
        report = DrainReport(outcome=DrainOutcome.DRAINED, waited_seconds=0.0)
        with mock.patch("teatree.loop.drain.drain_worker", side_effect=_quiescing_drain(report)):
            result = runner.invoke(worker_app, ["drain"])
        assert "worker_quiescing" in result.stdout
        assert "config_setting set worker_quiescing false" in result.stdout
        assert "t3 worker restart" in result.stdout

    def test_help_names_what_clears_the_quiesce(self) -> None:
        result = runner.invoke(worker_app, ["drain", "--help"])
        rendered = " ".join(result.stdout.split())
        assert "config_setting set worker_quiescing false" in rendered


class TestWorkerStop(django.test.TestCase):
    """`t3 worker stop` — drain, signal the flock holder, verify the exit, report the gate."""

    def test_stopped_exits_zero_and_names_the_pid(self) -> None:
        report = StopReport(outcome=StopOutcome.STOPPED, holder_pid=4242, waited_seconds=2.0)
        with mock.patch.object(WorkerStopper, "stop", return_value=report):
            result = runner.invoke(worker_app, ["stop"])
        assert result.exit_code == 0
        assert "stopped" in result.stdout
        assert "4242" in result.stdout

    def test_not_running_is_not_a_failure(self) -> None:
        with mock.patch.object(WorkerStopper, "stop", return_value=StopReport(outcome=StopOutcome.NOT_RUNNING)):
            result = runner.invoke(worker_app, ["stop"])
        assert result.exit_code == 0
        assert "not-running" in result.stdout

    def test_a_worker_that_refuses_to_exit_exits_non_zero(self) -> None:
        report = StopReport(outcome=StopOutcome.STILL_RUNNING, holder_pid=4242, waited_seconds=60.0)
        with mock.patch.object(WorkerStopper, "stop", return_value=report):
            result = runner.invoke(worker_app, ["stop"])
        assert result.exit_code == worker_cli._STOP_FAILED_EXIT
        assert result.exit_code != 0
        assert "still-running" in result.stdout
        assert "4242" in result.stdout

    def test_a_missing_holder_pid_is_reported_never_guessed(self) -> None:
        with mock.patch.object(WorkerStopper, "stop", return_value=StopReport(outcome=StopOutcome.NO_HOLDER_PID)):
            result = runner.invoke(worker_app, ["stop"])
        assert result.exit_code == worker_cli._STOP_FAILED_EXIT
        assert "no pid" in result.stdout

    def test_a_left_on_quiesce_is_stated_in_plain_words_with_the_recovery(self) -> None:
        report = StopReport(outcome=StopOutcome.STOPPED, holder_pid=4242, quiescing=True)
        with mock.patch.object(WorkerStopper, "stop", return_value=report):
            result = runner.invoke(worker_app, ["stop"])
        assert "admits ZERO new work" in result.stdout
        assert "config_setting set worker_quiescing false" in result.stdout
        assert "t3 worker restart" in result.stdout

    def test_json_shape(self) -> None:
        drain = DrainReport(outcome=DrainOutcome.GRACE_EXCEEDED, waited_seconds=30.0, still_claimed=[7])
        report = StopReport(outcome=StopOutcome.STOPPED, holder_pid=4242, drain=drain, waited_seconds=1.5)
        with mock.patch.object(WorkerStopper, "stop", return_value=report):
            result = runner.invoke(worker_app, ["stop", "--json"])
        payload = json.loads(result.stdout)
        assert payload["outcome"] == "stopped"
        assert payload["holder_pid"] == 4242
        assert payload["waited_seconds"] == pytest.approx(1.5)
        assert payload["worker_quiescing"] is False
        assert payload["drain"] == {"outcome": "grace_exceeded", "still_claimed": [7]}

    def test_no_drain_is_passed_through_to_the_stopper(self) -> None:
        seen: list[object] = []

        def _capture(self: WorkerStopper) -> StopReport:
            seen.append(self._request)
            return StopReport(outcome=StopOutcome.STOPPED, holder_pid=1)

        with mock.patch.object(WorkerStopper, "stop", _capture):
            result = runner.invoke(worker_app, ["stop", "--no-drain", "--timeout", "7", "--exit-timeout", "9"])
        assert result.exit_code == 0
        (request,) = seen
        assert request.drain is False
        assert request.drain_timeout == 7
        assert request.exit_timeout == pytest.approx(9.0)


class TestWorkerRestart(django.test.TestCase):
    """`t3 worker restart` — stop, respawn, and PROVE a new holder took the flock."""

    def test_reports_success_only_when_a_new_pid_holds_the_flock(self) -> None:
        stopped = StopReport(outcome=StopOutcome.STOPPED, holder_pid=4242)
        started = StartReport(started=True, holder_pid=999, waited_seconds=3.0)
        with (
            mock.patch.object(WorkerStopper, "stop", return_value=stopped),
            mock.patch.object(worker_cli, "_ensure_worker", return_value=("spawned", "spawned a detached worker")),
            mock.patch("teatree.loop.worker_lifecycle.wait_for_new_holder", return_value=started),
        ):
            result = runner.invoke(worker_app, ["restart"])
        assert result.exit_code == 0
        assert "restarted" in result.stdout
        assert "4242" in result.stdout
        assert "999" in result.stdout

    def test_a_spawn_that_never_takes_the_flock_is_a_failure(self) -> None:
        # `ensure` reports "spawned" as soon as the `t3` binary exists (the child's
        # streams go to DEVNULL), so a startup crash is invisible in its verdict — the
        # restart must verify independently and fail loudly.
        stopped = StopReport(outcome=StopOutcome.STOPPED, holder_pid=4242)
        never = StartReport(started=False, holder_pid=None, waited_seconds=60.0)
        with (
            mock.patch.object(WorkerStopper, "stop", return_value=stopped),
            mock.patch.object(worker_cli, "_ensure_worker", return_value=("spawned", "spawned a detached worker")),
            mock.patch("teatree.loop.worker_lifecycle.wait_for_new_holder", return_value=never),
            mock.patch(
                "teatree.utils.worker_spawn.read_spawn_log_tail", return_value="django.db.utils.OperationalError"
            ),
        ):
            result = runner.invoke(worker_app, ["restart"])
        assert result.exit_code == worker_cli._STOP_FAILED_EXIT
        assert "no worker holds the flock" in result.stdout
        assert "t3 worker status" in result.stdout
        # The crashed child's own output — the reason, not just the symptom.
        assert "OperationalError" in result.stdout

    def test_never_spawns_when_the_stop_failed(self) -> None:
        report = StopReport(outcome=StopOutcome.STILL_RUNNING, holder_pid=4242, waited_seconds=60.0)
        ensures: list[object] = []
        with (
            mock.patch.object(WorkerStopper, "stop", return_value=report),
            mock.patch.object(worker_cli, "_ensure_worker", side_effect=lambda: ensures.append(True)),
        ):
            result = runner.invoke(worker_app, ["restart"])
        assert result.exit_code == worker_cli._STOP_FAILED_EXIT
        assert ensures == []

    def test_clears_a_stuck_quiesce_gate_before_spawning(self) -> None:
        # The one-command recovery from the drain trap: whatever the gate was, the fresh
        # worker must come up admitting work.
        set_worker_quiescing(value=True)
        stopped = StopReport(outcome=StopOutcome.STOPPED, holder_pid=4242, quiescing=True)
        quiescing_at_spawn: list[bool] = []

        def _spawn() -> tuple[str, str]:
            quiescing_at_spawn.append(bool(ConfigSetting.objects.get_effective(QUIESCING_SETTING)))
            return "spawned", "spawned a detached worker"

        with (
            mock.patch.object(WorkerStopper, "stop", return_value=stopped),
            mock.patch.object(worker_cli, "_ensure_worker", side_effect=_spawn),
            mock.patch(
                "teatree.loop.worker_lifecycle.wait_for_new_holder",
                return_value=StartReport(started=True, holder_pid=999, waited_seconds=1.0),
            ),
        ):
            result = runner.invoke(worker_app, ["restart"])
        assert result.exit_code == 0
        assert quiescing_at_spawn == [False]
        assert ConfigSetting.objects.get_effective(QUIESCING_SETTING) is False

    def test_refuses_when_the_kill_switch_is_off(self) -> None:
        stopped = StopReport(outcome=StopOutcome.STOPPED, holder_pid=4242)
        with (
            mock.patch.object(WorkerStopper, "stop", return_value=stopped),
            mock.patch.object(worker_cli, "_ensure_worker", return_value=("disabled", "loop_runner_enabled is OFF")),
        ):
            result = runner.invoke(worker_app, ["restart"])
        assert result.exit_code == 1
        assert "disabled" in result.stdout


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


def test_emit_restart_json_shape(capsys: pytest.CaptureFixture[str]) -> None:
    worker_cli._emit_restart(json_output=True, action="restarted", detail="ok", previous_pid=10, new_pid=20)
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"action": "restarted", "detail": "ok", "previous_pid": 10, "holder_pid": 20}


def test_emit_stop_warns_when_the_drain_grace_lapsed(capsys: pytest.CaptureFixture[str]) -> None:
    report = StopReport(
        outcome=StopOutcome.STOPPED,
        holder_pid=1,
        waited_seconds=2.0,
        drain=DrainReport(outcome=DrainOutcome.GRACE_EXCEEDED, waited_seconds=30.0, still_claimed=[7, 9]),
    )
    worker_cli._emit_stop(report, json_output=False)
    out = capsys.readouterr().out
    assert "WARNING the drain grace lapsed" in out
    assert "7, 9" in out
