"""H24 self-heal doctor detectors — the silent-freeze classes made loud.

Each ``_check_*`` returns ``False`` (a hard FAIL that reddens ``t3 doctor``, and
so the external watchdog's ``t3 doctor --json``) when its silent-failure class is
present, and degrades to a pass when it cannot read the state — a self-heal
detector must never itself abort the doctor run.
"""

import base64
import datetime as dt
import io
import json as _json
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from typer.testing import CliRunner

from teatree.cli import app as cli_app
from teatree.cli.doctor import self_heal
from teatree.cli.doctor.self_heal import check_as_json, run_self_heal_checks
from teatree.config.agent_enums import AgentRuntime
from teatree.core.models import Ticket
from tests.factories import TaskFactory, TicketFactory

_MOD = "teatree.cli.doctor.self_heal"


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


class ComposeStackCheckTest(TestCase):
    def test_init_exited_nonzero_fails(self) -> None:
        states = [("teatree-init", "exited", "Exited (1) 2 minutes ago")]
        with mock.patch(f"{_MOD}._Probe.compose_container_states", return_value=states):
            ok, out = _echoes(self_heal._check_compose_stack)
        assert ok is False
        assert "FAIL" in out
        assert "teatree-init" in out

    def test_init_exited_zero_is_ok(self) -> None:
        states = [("teatree-init", "exited", "Exited (0) 2 minutes ago")]
        with mock.patch(f"{_MOD}._Probe.compose_container_states", return_value=states):
            ok, out = _echoes(self_heal._check_compose_stack)
        assert ok is True
        assert out == ""

    def test_worker_down_while_runner_on_fails(self) -> None:
        states = [("teatree-worker", "exited", "Exited (137) 1 minute ago")]
        with (
            mock.patch(f"{_MOD}._Probe.compose_container_states", return_value=states),
            mock.patch(f"{_MOD}._Probe.loop_runner_on", return_value=True),
        ):
            ok, out = _echoes(self_heal._check_compose_stack)
        assert ok is False
        assert "teatree-worker" in out

    def test_worker_down_while_runner_off_is_ok(self) -> None:
        states = [("teatree-worker", "exited", "Exited (0)")]
        with (
            mock.patch(f"{_MOD}._Probe.compose_container_states", return_value=states),
            mock.patch(f"{_MOD}._Probe.loop_runner_on", return_value=False),
        ):
            ok, _out = _echoes(self_heal._check_compose_stack)
        assert ok is True

    def test_docker_unavailable_degrades_to_pass(self) -> None:
        with mock.patch(f"{_MOD}._Probe.compose_container_states", return_value=None):
            ok, out = _echoes(self_heal._check_compose_stack)
        assert ok is True
        assert out == ""

    def test_all_running_is_ok(self) -> None:
        states = [
            ("teatree-init", "exited", "Exited (0)"),
            ("teatree-worker", "running", "Up 3 hours"),
            ("teatree-admin", "running", "Up 3 hours"),
        ]
        with (
            mock.patch(f"{_MOD}._Probe.compose_container_states", return_value=states),
            mock.patch(f"{_MOD}._Probe.loop_runner_on", return_value=True),
        ):
            ok, _out = _echoes(self_heal._check_compose_stack)
        assert ok is True


class ComposeStackWatchdogHandoffTest(TestCase):
    """The compose-stack detector must WORK where it runs, not silently pass.

    ``t3 doctor`` runs inside ``teatree-admin``, which has the ``docker`` CLI but
    NO ``/var/run/docker.sock`` (only the watchdog mounts the socket), so a local
    ``docker ps`` fails and the detector used to return ``None`` -> pass, making a
    crash-looping init / down worker undetectable in production (dead code). The
    socket-holding watchdog now hands the states in via the
    ``TEATREE_DOCTOR_COMPOSE_PS`` env var (base64 of the same ``docker ps`` output);
    the probe reads that handoff even when the local ``docker`` cannot reach the
    daemon, so a real outage FAILs and reaches the owner DM.
    """

    @staticmethod
    def _handoff(rows: list[tuple[str, str, str]]) -> str:
        text = "\n".join("\t".join(row) for row in rows)
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    def test_handoff_states_used_when_local_docker_unreachable(self) -> None:
        # No local docker socket (CLI absent stands in for "cannot reach daemon"),
        # yet the watchdog handoff carries a crash-looped init: the probe must
        # return those states instead of None. RED before the handoff branch.
        env = {"TEATREE_DOCTOR_COMPOSE_PS": self._handoff([("teatree-init", "exited", "Exited (1) ago")])}
        with (
            mock.patch(f"{_MOD}.shutil.which", return_value=None),
            mock.patch.dict("os.environ", env, clear=False),
        ):
            states = self_heal._Probe.compose_container_states("teatree")
        assert states == [("teatree-init", "exited", "Exited (1) ago")]

    def test_down_stack_fails_via_handoff_without_socket(self) -> None:
        # End to end: no socket + a handoff-supplied crash-looped init -> the
        # detector FAILs (does not silently pass), so the watchdog DMs the owner.
        env = {"TEATREE_DOCTOR_COMPOSE_PS": self._handoff([("teatree-init", "exited", "Exited (1) ago")])}
        with (
            mock.patch(f"{_MOD}.shutil.which", return_value=None),
            mock.patch.dict("os.environ", env, clear=False),
        ):
            ok, out = _echoes(self_heal._check_compose_stack)
        assert ok is False
        assert "teatree-init" in out

    def test_no_handoff_and_no_socket_still_degrades_to_pass(self) -> None:
        # Anti-over-block: a dev box (no watchdog handoff, no socket) must still
        # degrade to a pass, never a false FAIL.
        with (
            mock.patch(f"{_MOD}.shutil.which", return_value=None),
            mock.patch.dict("os.environ", {"TEATREE_DOCTOR_COMPOSE_PS": ""}, clear=False),
        ):
            states = self_heal._Probe.compose_container_states("teatree")
        assert states is None

    def test_malformed_handoff_falls_back_to_none(self) -> None:
        # A corrupt base64 handoff must not crash; with no local socket it yields
        # None (degrade to pass), never a partial/garbage verdict.
        with (
            mock.patch(f"{_MOD}.shutil.which", return_value=None),
            mock.patch.dict("os.environ", {"TEATREE_DOCTOR_COMPOSE_PS": "!!!not base64!!!"}, clear=False),
        ):
            states = self_heal._Probe.compose_container_states("teatree")
        assert states is None


class LoopWorkerAliveCheckTest(TestCase):
    def test_free_flock_over_overdue_work_fails(self) -> None:
        overdue = [("inbox", timezone.now() - dt.timedelta(hours=1), 600)]
        with (
            mock.patch(f"{_MOD}._Probe.loop_runner_on", return_value=True),
            mock.patch(f"{_MOD}._Probe.worker_flock_free", return_value=True),
            mock.patch(f"{_MOD}._Probe.overdue_ready_timers", return_value=overdue),
        ):
            ok, out = _echoes(self_heal._check_loop_worker_alive)
        assert ok is False
        assert "inbox" in out
        assert "worker" in out.lower()

    def test_free_flock_but_no_overdue_work_is_ok(self) -> None:
        with (
            mock.patch(f"{_MOD}._Probe.loop_runner_on", return_value=True),
            mock.patch(f"{_MOD}._Probe.worker_flock_free", return_value=True),
            mock.patch(f"{_MOD}._Probe.overdue_ready_timers", return_value=[]),
        ):
            ok, _out = _echoes(self_heal._check_loop_worker_alive)
        assert ok is True

    def test_held_flock_is_ok(self) -> None:
        with (
            mock.patch(f"{_MOD}._Probe.loop_runner_on", return_value=True),
            mock.patch(f"{_MOD}._Probe.worker_flock_free", return_value=False),
        ):
            ok, _out = _echoes(self_heal._check_loop_worker_alive)
        assert ok is True

    def test_runner_off_is_ok(self) -> None:
        with mock.patch(f"{_MOD}._Probe.loop_runner_on", return_value=False):
            ok, _out = _echoes(self_heal._check_loop_worker_alive)
        assert ok is True

    def test_crash_degrades_to_pass(self) -> None:
        with mock.patch(f"{_MOD}._Probe.loop_runner_on", side_effect=RuntimeError("boom")):
            ok, out = _echoes(self_heal._check_loop_worker_alive)
        assert ok is True
        assert "WARN" in out


class StrandedHeadlessCheckTest(TestCase):
    def test_running_headless_with_free_flock_fails(self) -> None:
        stranded = [("501", timezone.now() - dt.timedelta(hours=2))]
        with (
            mock.patch(f"{_MOD}._Probe.worker_flock_free", return_value=True),
            mock.patch(f"{_MOD}._Probe.stranded_headless_results", return_value=stranded),
        ):
            ok, out = _echoes(self_heal._check_stranded_headless_task)
        assert ok is False
        assert "501" in out

    def test_worker_alive_is_ok(self) -> None:
        with mock.patch(f"{_MOD}._Probe.worker_flock_free", return_value=False):
            ok, _out = _echoes(self_heal._check_stranded_headless_task)
        assert ok is True

    def test_no_stranded_rows_is_ok(self) -> None:
        with (
            mock.patch(f"{_MOD}._Probe.worker_flock_free", return_value=True),
            mock.patch(f"{_MOD}._Probe.stranded_headless_results", return_value=[]),
        ):
            ok, _out = _echoes(self_heal._check_stranded_headless_task)
        assert ok is True


class StaleLoopTimerCheckTest(TestCase):
    def test_overdue_timer_fails(self) -> None:
        due = timezone.now() - dt.timedelta(minutes=30)
        with mock.patch(f"{_MOD}._Probe.overdue_ready_timers", return_value=[("review", due, 600)]):
            ok, out = _echoes(self_heal._check_stale_loop_timer)
        assert ok is False
        assert "review" in out

    def test_no_overdue_timer_is_ok(self) -> None:
        with mock.patch(f"{_MOD}._Probe.overdue_ready_timers", return_value=[]):
            ok, _out = _echoes(self_heal._check_stale_loop_timer)
        assert ok is True


class InteractiveUnderHeadlessCheckTest(TestCase):
    def test_pending_interactive_task_under_headless_fails(self) -> None:
        TaskFactory(status="pending", execution_target="interactive")
        headless = SimpleNamespace(agent_runtime=AgentRuntime.HEADLESS)
        with mock.patch("teatree.config.get_effective_settings", return_value=headless):
            ok, out = _echoes(self_heal._check_interactive_task_under_headless)
        assert ok is False
        assert "headless" in out.lower()

    def test_interactive_runtime_is_ok(self) -> None:
        TaskFactory(execution_target="interactive")
        interactive = SimpleNamespace(agent_runtime=AgentRuntime.INTERACTIVE)
        with mock.patch("teatree.config.get_effective_settings", return_value=interactive):
            ok, _out = _echoes(self_heal._check_interactive_task_under_headless)
        assert ok is True

    def test_no_interactive_tasks_under_headless_is_ok(self) -> None:
        headless = SimpleNamespace(agent_runtime=AgentRuntime.HEADLESS)
        with mock.patch("teatree.config.get_effective_settings", return_value=headless):
            ok, _out = _echoes(self_heal._check_interactive_task_under_headless)
        assert ok is True


class FailedTaskOnLiveTicketCheckTest(TestCase):
    def test_failed_task_on_live_ticket_fails(self) -> None:
        ticket = TicketFactory(state=Ticket.State.CODED)
        TaskFactory(ticket=ticket, status="failed", execution_target="interactive")
        ok, out = _echoes(self_heal._check_failed_tasks_on_live_tickets)
        assert ok is False
        assert f"#{ticket.ticket_number}" in out

    def test_failed_task_on_terminal_ticket_is_ok(self) -> None:
        ticket = TicketFactory(state=Ticket.State.MERGED)
        TaskFactory(ticket=ticket, status="failed", execution_target="interactive")
        ok, _out = _echoes(self_heal._check_failed_tasks_on_live_tickets)
        assert ok is True

    def test_no_failed_tasks_is_ok(self) -> None:
        ticket = TicketFactory(state=Ticket.State.CODED)
        TaskFactory(ticket=ticket, status="pending", execution_target="interactive")
        ok, _out = _echoes(self_heal._check_failed_tasks_on_live_tickets)
        assert ok is True


class RuntimeCloneBranchCheckTest(TestCase):
    def test_drifted_branch_fails(self) -> None:
        root = Path("/home/teatree/teatree")
        with (
            mock.patch(f"{_MOD}._Probe.runtime_clone_root", return_value=root),
            mock.patch("teatree.utils.git.current_branch", return_value="feat/stray"),
            mock.patch("teatree.utils.git.default_branch", return_value="main"),
        ):
            ok, out = _echoes(self_heal._check_runtime_clone_on_default_branch)
        assert ok is False
        assert "main" in out
        assert "feat/stray" in out

    def test_on_default_branch_is_ok(self) -> None:
        with (
            mock.patch(f"{_MOD}._Probe.runtime_clone_root", return_value=Path("/home/teatree/teatree")),
            mock.patch("teatree.utils.git.current_branch", return_value="main"),
            mock.patch("teatree.utils.git.default_branch", return_value="main"),
        ):
            ok, _out = _echoes(self_heal._check_runtime_clone_on_default_branch)
        assert ok is True

    def test_unresolvable_clone_degrades_to_pass(self) -> None:
        with mock.patch(f"{_MOD}._Probe.runtime_clone_root", return_value=None):
            ok, _out = _echoes(self_heal._check_runtime_clone_on_default_branch)
        assert ok is True


class RunAllAndJsonTest(TestCase):
    def test_run_self_heal_checks_false_when_one_fails(self) -> None:
        with mock.patch(f"{_MOD}._check_stale_loop_timer", return_value=False), redirect_stdout(io.StringIO()):
            assert run_self_heal_checks() is False

    def test_run_self_heal_checks_true_when_all_pass(self) -> None:
        names = (
            "_check_compose_stack",
            "_check_loop_worker_alive",
            "_check_stranded_headless_task",
            "_check_stale_loop_timer",
            "_check_interactive_task_under_headless",
            "_check_failed_tasks_on_live_tickets",
            "_check_runtime_clone_on_default_branch",
        )
        with mock.patch.multiple(_MOD, **dict.fromkeys(names, mock.DEFAULT)) as mocks:
            for m in mocks.values():
                m.return_value = True
            assert run_self_heal_checks() is True

    def test_check_as_json_emits_ok_and_findings(self) -> None:
        def fake_check() -> bool:
            print("FAIL  the worker is down")  # noqa: T201 — the doctor echo the JSON surface parses
            print("OK    everything else")  # noqa: T201 — the doctor echo the JSON surface parses
            return False

        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = check_as_json(fake_check)
        payload = _json.loads(buf.getvalue())
        assert ok is False
        assert payload["ok"] is False
        levels = {f["level"] for f in payload["findings"]}
        assert "FAIL" in levels
        assert any(f["message"] == "the worker is down" for f in payload["findings"])


class DoctorJsonSurfaceTest(TestCase):
    """`--json` routes to the JSON surface; a subcommand-only call never does."""

    def test_json_flag_routes_to_check_as_json(self) -> None:
        def _emit(_run) -> bool:
            print('{"ok": true, "findings": []}')  # noqa: T201 — the JSON surface under test
            return True

        with mock.patch(f"{_MOD}.check_as_json", side_effect=_emit) as spy:
            result = CliRunner().invoke(cli_app, ["doctor", "check", "--json"])
        assert spy.called
        assert '"ok": true' in result.output

    def test_json_without_repair_threads_repair_false(self) -> None:
        """`--json` alone must not run the checks with repair implicitly enabled (#3313).

        The watchdog's unattended `t3 doctor check --json` re-pointed the global
        editable install because the JSON path re-invoked the checks with the
        `--repair` OptionInfo sentinel (truthy). The JSON callable now threads the
        resolved `repair=False`.
        """
        import teatree.cli.doctor.app as doctor_app_mod  # noqa: PLC0415

        captured: dict[str, bool] = {}

        def _run_checks(*, repair: bool = False, slack_roundtrip: bool = False) -> bool:
            captured["repair"] = repair
            captured["slack_roundtrip"] = slack_roundtrip
            return True

        with mock.patch.object(doctor_app_mod, "run_doctor_checks", side_effect=_run_checks):
            result = CliRunner().invoke(cli_app, ["doctor", "check", "--json"])
        assert captured["repair"] is False
        assert captured["slack_roundtrip"] is False
        assert result.exit_code == 0

    def test_json_with_repair_threads_repair_true(self) -> None:
        """`--json --repair` threads the resolved `repair=True` through the JSON path."""
        import teatree.cli.doctor.app as doctor_app_mod  # noqa: PLC0415

        captured: dict[str, bool] = {}

        def _run_checks(*, repair: bool = False, slack_roundtrip: bool = False) -> bool:
            captured["repair"] = repair
            captured["slack_roundtrip"] = slack_roundtrip
            return True

        with mock.patch.object(doctor_app_mod, "run_doctor_checks", side_effect=_run_checks):
            CliRunner().invoke(cli_app, ["doctor", "check", "--json", "--repair"])
        assert captured["repair"] is True
        assert captured["slack_roundtrip"] is False

    def test_json_with_slack_roundtrip_threads_true(self) -> None:
        """`--json --slack-roundtrip` threads `slack_roundtrip=True` without disturbing `repair` (#3411)."""
        import teatree.cli.doctor.app as doctor_app_mod  # noqa: PLC0415

        captured: dict[str, bool] = {}

        def _run_checks(*, repair: bool = False, slack_roundtrip: bool = False) -> bool:
            captured["repair"] = repair
            captured["slack_roundtrip"] = slack_roundtrip
            return True

        with mock.patch.object(doctor_app_mod, "run_doctor_checks", side_effect=_run_checks):
            CliRunner().invoke(cli_app, ["doctor", "check", "--json", "--slack-roundtrip"])
        assert captured["slack_roundtrip"] is True
        assert captured["repair"] is False

    def test_check_without_json_does_not_route_to_json(self) -> None:
        with (
            mock.patch(f"{_MOD}.check_as_json") as spy,
            mock.patch(f"{_MOD}.run_self_heal_checks", return_value=True),
        ):
            CliRunner().invoke(cli_app, ["doctor", "check"])
        assert not spy.called


class ParseFindingsTest(TestCase):
    def test_levels_and_messages_split(self) -> None:
        text = "FAIL  boom\nWARN  careful\nOK    fine\nAll checks passed\n"
        findings = self_heal._Probe.parse_findings(text)
        assert findings[0] == {"level": "FAIL", "message": "boom"}
        assert findings[1] == {"level": "WARN", "message": "careful"}
        assert findings[2] == {"level": "OK", "message": "fine"}
        assert findings[3] == {"level": "INFO", "message": "All checks passed"}

    def test_blank_lines_skipped(self) -> None:
        assert self_heal._Probe.parse_findings("\n\n  \n") == []


class SlackDrainCheckTest(TestCase):
    """`_check_slack_drain_alive` reads the sidecar heartbeat another container writes.

    Functional: writes a real heartbeat JSON to a tmp DATA_DIR and runs the check,
    so the probe's parse and the FAIL/degrade logic are exercised together.
    """

    def setUp(self) -> None:
        import tempfile  # noqa: PLC0415 — test-local

        self._dir = tempfile.mkdtemp()
        self._patch = mock.patch(f"{_MOD}.DATA_DIR", Path(self._dir))
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write_beat(self, *, age_seconds: int, consecutive: int, interval: int = 15) -> None:
        import time  # noqa: PLC0415 — test-local

        beat = {
            "updated_at": int(time.time()) - age_seconds,
            "interval_seconds": interval,
            "consecutive_failures": consecutive,
            "last_ok_at": int(time.time()) - age_seconds,
        }
        (Path(self._dir) / "slack-drain-heartbeat.json").write_text(_json.dumps(beat), encoding="utf-8")

    def test_absent_heartbeat_degrades_to_pass(self) -> None:
        ok, out = _echoes(self_heal._check_slack_drain_alive)
        assert ok is True
        assert out == ""

    def test_fresh_healthy_heartbeat_is_ok(self) -> None:
        self._write_beat(age_seconds=5, consecutive=0)
        ok, out = _echoes(self_heal._check_slack_drain_alive)
        assert ok is True
        assert out == ""

    def test_stale_heartbeat_fails(self) -> None:
        # No refresh for well past max(4x interval, 120s) — the drain loop died/hung.
        self._write_beat(age_seconds=600, consecutive=0)
        ok, out = _echoes(self_heal._check_slack_drain_alive)
        assert ok is False
        assert "FAIL" in out
        assert "stale" in out

    def test_consecutive_failures_fail(self) -> None:
        self._write_beat(age_seconds=5, consecutive=self_heal._MAX_DRAIN_CONSECUTIVE_FAILURES)
        ok, out = _echoes(self_heal._check_slack_drain_alive)
        assert ok is False
        assert "FAIL" in out
        assert "failed" in out

    def test_a_few_failures_but_fresh_is_ok(self) -> None:
        # Below the threshold and freshly beating — a transient blip, not a break.
        self._write_beat(age_seconds=5, consecutive=self_heal._MAX_DRAIN_CONSECUTIVE_FAILURES - 1)
        ok, out = _echoes(self_heal._check_slack_drain_alive)
        assert ok is True
        assert out == ""

    def test_unparsable_heartbeat_degrades_to_pass(self) -> None:
        (Path(self._dir) / "slack-drain-heartbeat.json").write_text("{not json", encoding="utf-8")
        ok, out = _echoes(self_heal._check_slack_drain_alive)
        assert ok is True
        assert out == ""
