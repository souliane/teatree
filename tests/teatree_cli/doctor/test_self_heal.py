"""H24 self-heal doctor detectors — the silent-freeze classes made loud.

Each ``_check_*`` returns ``False`` (a hard FAIL that reddens ``t3 doctor``, and
so the external watchdog's ``t3 doctor --json``) when its silent-failure class is
present, and degrades to a pass when it cannot read the state — a self-heal
detector must never itself abort the doctor run.
"""

import ast
import base64
import datetime as dt
import io
import json as _json
import os
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from typer.testing import CliRunner

from teatree.cli import app as cli_app
from teatree.cli.doctor import self_heal
from teatree.cli.doctor.self_heal import check_as_json, run_self_heal_checks
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
            ("teatree-watchdog", "running", "Up 3 hours"),
        ]
        with (
            mock.patch(f"{_MOD}._Probe.compose_container_states", return_value=states),
            mock.patch(f"{_MOD}._Probe.loop_runner_on", return_value=True),
        ):
            ok, _out = _echoes(self_heal._check_compose_stack)
        assert ok is True

    def test_watchdog_stuck_created_fails(self) -> None:
        # The supervisor is the one container nothing else restarts, so a watchdog
        # that never started (an unmountable bind source, say) silently removes the
        # alerting for every other finding in this module. Leaving it off the
        # long-running set is what made that blind spot unreportable.
        states = [
            ("teatree-worker", "running", "Up 3 hours"),
            ("teatree-watchdog", "created", "Created"),
        ]
        with (
            mock.patch(f"{_MOD}._Probe.compose_container_states", return_value=states),
            mock.patch(f"{_MOD}._Probe.loop_runner_on", return_value=True),
        ):
            ok, out = _echoes(self_heal._check_compose_stack)
        assert ok is False
        assert "teatree-watchdog" in out

    def test_every_long_running_service_is_watched(self) -> None:
        assert set(self_heal._LONG_RUNNING_SERVICES) == {
            "teatree-worker",
            "teatree-admin",
            "teatree-watchdog",
        }


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


class StaleLoopTimerCollapseTest(TestCase):
    """Overdue timers collapse to one timestamp-free FAIL summary (#slack-comms Phase 3)."""

    def test_many_overdue_timers_render_one_fail_line(self) -> None:
        overdue = [
            ("inbox", timezone.now() - dt.timedelta(hours=1), 600),
            ("ship", timezone.now() - dt.timedelta(hours=2), 600),
            ("review", timezone.now() - dt.timedelta(hours=3), 600),
        ]
        with mock.patch(f"{_MOD}._Probe.overdue_ready_timers", return_value=overdue):
            ok, out = _echoes(self_heal._check_stale_loop_timer)
        assert ok is False
        fail_lines = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
        assert len(fail_lines) == 1
        # the set of names is named on the one summary line
        for name in ("inbox", "review", "ship"):
            assert name in fail_lines[0]

    def test_fail_summary_has_no_volatile_timestamp(self) -> None:
        # The watchdog RED body-hash keys on FAIL messages; an isoformat timestamp
        # there would churn the key every pass. Timestamps live on non-FAIL detail.
        run_after = timezone.now() - dt.timedelta(hours=5)
        overdue = [("inbox", run_after, 600)]
        with mock.patch(f"{_MOD}._Probe.overdue_ready_timers", return_value=overdue):
            _ok, out = _echoes(self_heal._check_stale_loop_timer)
        fail_lines = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
        assert fail_lines
        assert run_after.isoformat() not in fail_lines[0]
        # the timestamp detail is still surfaced (on a non-FAIL line)
        assert run_after.isoformat() in out


class StrandedHeadlessCheckTest(TestCase):
    def test_running_headless_with_free_flock_fails(self) -> None:
        stranded = [("501", timezone.now() - dt.timedelta(hours=2))]
        with (
            mock.patch(f"{_MOD}._Probe.worker_flock_free", return_value=True),
            mock.patch(f"{_MOD}._Probe.stranded_runner_results", return_value=stranded),
        ):
            ok, out = _echoes(self_heal._check_stranded_task)
        assert ok is False
        assert "501" in out

    def test_worker_alive_is_ok(self) -> None:
        with mock.patch(f"{_MOD}._Probe.worker_flock_free", return_value=False):
            ok, _out = _echoes(self_heal._check_stranded_task)
        assert ok is True

    def test_no_stranded_rows_is_ok(self) -> None:
        with (
            mock.patch(f"{_MOD}._Probe.worker_flock_free", return_value=True),
            mock.patch(f"{_MOD}._Probe.stranded_runner_results", return_value=[]),
        ):
            ok, _out = _echoes(self_heal._check_stranded_task)
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


class FailedTaskOnLiveTicketCheckTest(TestCase):
    def test_failed_task_on_live_ticket_fails(self) -> None:
        ticket = TicketFactory(state=Ticket.State.CODED)
        TaskFactory(ticket=ticket, status="failed")
        ok, out = _echoes(self_heal._check_failed_tasks_on_live_tickets)
        assert ok is False
        assert f"#{ticket.ticket_number}" in out

    def test_failed_task_on_terminal_ticket_is_ok(self) -> None:
        ticket = TicketFactory(state=Ticket.State.MERGED)
        TaskFactory(ticket=ticket, status="failed")
        ok, _out = _echoes(self_heal._check_failed_tasks_on_live_tickets)
        assert ok is True

    def test_no_failed_tasks_is_ok(self) -> None:
        ticket = TicketFactory(state=Ticket.State.CODED)
        TaskFactory(ticket=ticket, status="pending")
        ok, _out = _echoes(self_heal._check_failed_tasks_on_live_tickets)
        assert ok is True

    def test_failed_task_on_cadence_anchor_is_ok(self) -> None:
        # souliane/teatree#3492: a `<scheme>://<overlay>` loop-cadence anchor is a
        # recurring schedule, not deliverable work — it can never reach a terminal
        # state, so any cadence tick that ever failed pins the FAIL forever.
        ticket = TicketFactory(state=Ticket.State.NOT_STARTED, issue_url="scanning-news://t3-teatree")
        TaskFactory(ticket=ticket, status="failed")
        ok, _out = _echoes(self_heal._check_failed_tasks_on_live_tickets)
        assert ok is True

    def test_failed_task_with_a_successor_is_ok(self) -> None:
        # souliane/teatree#4357: a ticket that was re-dispatched after the failure is being
        # advanced, so naming it forever is what grew the line to 44 unactionable tickets.
        ticket = TicketFactory(state=Ticket.State.CODED)
        TaskFactory(ticket=ticket, status="failed")
        TaskFactory(ticket=ticket, status="pending")
        ok, _out = _echoes(self_heal._check_failed_tasks_on_live_tickets)
        assert ok is True

    def test_failure_after_a_successful_task_is_still_reported(self) -> None:
        # The inverse ordering: the newest task IS the failure, so nothing succeeded it.
        ticket = TicketFactory(state=Ticket.State.CODED)
        TaskFactory(ticket=ticket, status="completed")
        TaskFactory(ticket=ticket, status="failed")
        ok, out = _echoes(self_heal._check_failed_tasks_on_live_tickets)
        assert ok is False
        assert f"#{ticket.ticket_number}" in out

    def test_failed_task_on_bare_number_row_is_ok(self) -> None:
        # souliane/teatree#3492: a bare-number `issue_url` is malformed debris from
        # a write path closed by #3289. `derive_issue_number` still renders it as a
        # forge-looking `#3274`, which is what made it read as frozen issue work.
        ticket = TicketFactory(state=Ticket.State.STARTED, issue_url="3274", overlay="")
        TaskFactory(ticket=ticket, status="failed")
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


class RuntimeCloneResolutionTest(TestCase):
    """#4339: the clone is resolved from what the deployment declares, never hard-coded.

    The hard-coded box path was absent on the box, so the lookup degraded to ``None``
    and the drift detector was silently inert — the failure mode that goes unnoticed
    precisely because nothing crashes.
    """

    _ABSENT = Path("/nonexistent-runtime-clone")

    def test_the_declared_clone_dir_wins(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            with mock.patch.dict(os.environ, {"TEATREE_CLONE_DIR": str(root)}, clear=True):
                assert self_heal._Probe.runtime_clone_root() == root

    def test_the_deploy_checkout_is_the_next_candidate(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            env = {"TEATREE_CLONE_DIR": str(root / "absent"), "TEATREE_DEPLOY_CHECKOUT": str(root)}
            with mock.patch.dict(os.environ, env, clear=True):
                assert self_heal._Probe.runtime_clone_root() == root

    def test_a_venue_declaring_none_resolves_nothing(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(self_heal, "_BOX_RUNTIME_CLONE", self._ABSENT),
        ):
            assert self_heal._Probe.declared_runtime_clones() == []
            assert self_heal._Probe.runtime_clone_root() is None

    def test_a_declared_but_unresolvable_clone_is_reported(self) -> None:
        with (
            mock.patch.dict(os.environ, {"TEATREE_CLONE_DIR": str(self._ABSENT)}, clear=True),
            mock.patch.object(self_heal, "_BOX_RUNTIME_CLONE", self._ABSENT),
        ):
            ok, out = _echoes(self_heal._check_runtime_clone_on_default_branch)
        assert ok is True, "an inert detector must not redden the run"
        assert out.startswith("WARN")
        assert str(self._ABSENT) in out

    def test_a_venue_declaring_none_stays_silent(self) -> None:
        # The anti-vacuous control: a dev machine with no H24 clone is not a misconfiguration.
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(self_heal, "_BOX_RUNTIME_CLONE", self._ABSENT),
        ):
            ok, out = _echoes(self_heal._check_runtime_clone_on_default_branch)
        assert ok is True
        assert out == ""


class RunAllAndJsonTest(TestCase):
    def test_run_self_heal_checks_false_when_one_fails(self) -> None:
        with mock.patch(f"{_MOD}._check_stale_loop_timer", return_value=False), redirect_stdout(io.StringIO()):
            assert run_self_heal_checks() is False

    def test_run_self_heal_checks_true_when_all_pass(self) -> None:
        names = (
            "_check_compose_stack",
            "_check_loop_worker_alive",
            "_check_stranded_task",
            "_check_stale_loop_timer",
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

    def test_a_crashing_check_still_emits_a_verdict_naming_the_cause(self) -> None:
        # The doctor plane went dark for 72 watchdog passes because `packaging` was an
        # undeclared runtime dep: the eager import raised, `run_checks()` never returned,
        # and the JSON line — emitted only AFTER it returns — was never written at all.
        # Zero bytes is the one verdict the watchdog cannot name a cause for, so a crash
        # must degrade to a RED verdict that carries its own exception text.
        boom = ModuleNotFoundError("No module named 'packaging'")

        def crashing_check() -> bool:
            print("OK    the checks that ran before the crash")  # noqa: T201 — the doctor echo the JSON surface parses
            raise boom

        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = check_as_json(crashing_check)

        payload = _json.loads(buf.getvalue())
        assert ok is False
        assert payload["ok"] is False
        crash = [f for f in payload["findings"] if f["level"] == "FAIL" and "packaging" in f["message"]]
        assert crash, payload["findings"]
        assert "ModuleNotFoundError" in crash[0]["message"]
        # The echoes the run DID produce before dying survive alongside the crash line.
        assert any(f["message"] == "the checks that ran before the crash" for f in payload["findings"])


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
        """The bare subcommand takes the non-JSON branch, so ``check_as_json`` stays untouched.

        ``run_doctor_checks`` is stubbed for the same reason every ``--json`` sibling above
        stubs it: the assertion is about which branch ``check`` picks, and the real aggregate
        answers a different question at the price of every check it owns — token-permission
        probes, the Slack round-trip and a ``claude mcp list`` subprocess among them. Stubbing
        the nested ``run_self_heal_checks`` alone left the rest of that aggregate live on the
        one test in this class that does not pass ``--json``, which is how a pure routing
        assertion came to exceed the global 60s ``pytest-timeout`` under shard contention
        (#4048).
        """
        import teatree.cli.doctor.app as doctor_app_mod  # noqa: PLC0415 — deferred, as its siblings (#4048)

        with (
            mock.patch(f"{_MOD}.check_as_json") as spy,
            mock.patch.object(doctor_app_mod, "run_doctor_checks", return_value=True),
        ):
            CliRunner().invoke(cli_app, ["doctor", "check"])
        assert not spy.called


class ParseFindingsTest(TestCase):
    def test_levels_and_messages_split(self) -> None:
        text = "FAIL  boom\nWARN  careful\nOK    fine\nAll checks passed\n"
        findings = self_heal._Probe.parse_findings(text)
        assert [(f["level"], f["message"]) for f in findings] == [
            ("FAIL", "boom"),
            ("WARN", "careful"),
            ("OK", "fine"),
            ("INFO", "All checks passed"),
        ]

    def test_blank_lines_skipped(self) -> None:
        assert self_heal._Probe.parse_findings("\n\n  \n") == []

    def test_each_finding_carries_its_volatility_normalized_identity(self) -> None:
        """The watchdog keys its owner DM on identities, so the doctor must emit them."""
        before = self_heal._Probe.parse_findings("FAIL  clone is 17 commit(s) behind origin/main\n")
        after = self_heal._Probe.parse_findings("FAIL  clone is 18 commit(s) behind origin/main\n")
        assert before[0]["identity"] == after[0]["identity"]
        assert before[0]["message"] != after[0]["message"]


def _detector_docstring_bullets() -> list[str]:
    """The module docstring's detector list, each bullet folded onto one line."""
    bullets: list[str] = []
    for line in (self_heal.__doc__ or "").splitlines():
        if line.startswith("- "):
            bullets.append(line)
        elif bullets and line.startswith("    "):
            bullets[-1] += " " + line.strip()
    return bullets


def _wired_check_expressions() -> list[str]:
    """The checks ``run_self_heal_checks`` actually runs, read off its own source."""
    source = Path(str(self_heal.__file__)).read_text(encoding="utf-8")
    wired = [
        [ast.unparse(element) for element in node.value.elts]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "checks"
    ]

    assert wired, "run_self_heal_checks no longer declares its `checks` tuple"
    return wired[0]


class DetectorRepairAnnotationTest(TestCase):
    """The asymmetry between repairing and reporting detectors must be readable, not grepped."""

    def test_every_detector_says_whether_it_repairs_or_only_reports(self) -> None:
        unannotated = [
            bullet for bullet in _detector_docstring_bullets() if "REPAIRS" not in bullet and "REPORTS" not in bullet
        ]

        assert not unannotated, f"annotate these detectors REPAIRS/REPORTS: {unannotated}"

    def test_the_annotated_list_covers_every_wired_detector(self) -> None:
        # A detector added to the sequence but not to the list would leave the asymmetry
        # invisible again — which is the whole defect the annotation exists to end.
        assert len(_detector_docstring_bullets()) == len(_wired_check_expressions())
