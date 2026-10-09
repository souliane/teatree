"""`t3 doctor check` names a compose service stuck in a restart loop the watchdog cannot see.

The watchdog only revives an exited service, so a service that keeps exiting cleanly and
being restarted by Docker's ``unless-stopped`` policy reads as healthy to it: the worker
restarted 23,155 times that way. Docker zeroes ``State.ExitCode`` whenever a container
starts, so a RUNNING container always reads exit 0 — only a restarting/exited one carries
its real last exit.
"""

import datetime as dt
import subprocess
from unittest.mock import patch

import pytest

from teatree.cli.doctor import checks_runtime, run_checks
from teatree.cli.doctor.checks_runtime import _check_clean_exit_restart_loop, _inspect_compose_containers
from teatree.utils.run import CompletedProcess


def _started(seconds_ago: int) -> str:
    return (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


def _row(service: str, restarts: int, exit_code: int, started_at: str, status: str) -> str:
    return f"{service}\t{restarts}\t{exit_code}\t{started_at}\t{status}"


def _check(rows: list[str] | None, capsys: pytest.CaptureFixture[str]) -> tuple[bool, str]:
    ok = _check_clean_exit_restart_loop(inspect=lambda: rows)
    return ok, capsys.readouterr().out


class TestTheVerdict:
    def test_a_running_service_restarted_thousands_of_times_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        ok, out = _check([_row("teatree-worker", 23155, 0, _started(5), "running")], capsys)
        assert not ok
        assert out.startswith("FAIL")
        assert "teatree-worker restarted 23155 times" in out
        assert "t3 loop preset show" in out
        assert "last exit 0" not in out

    def test_a_restarting_service_whose_last_exit_was_clean_fails_naming_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ok, out = _check([_row("acme-worker", 40, 0, _started(20), "restarting")], capsys)
        assert not ok
        assert "acme-worker restarted 40 times, last exit 0" in out

    def test_a_crash_loop_is_not_this_finding(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _check([_row("teatree-worker", 23155, 1, _started(5), "restarting")], capsys) == (True, "")

    def test_restarts_accumulated_long_ago_are_not_a_loop(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _check([_row("teatree-admin", 25, 0, _started(2 * 3600), "running")], capsys) == (True, "")

    def test_a_few_recent_restarts_are_not_a_loop(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _check([_row("teatree-admin", 19, 0, _started(5), "running")], capsys) == (True, "")

    def test_a_never_started_container_is_not_within_the_window(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert _check([_row("teatree-init", 30, 0, "0001-01-01T00:00:00Z", "created")], capsys) == (True, "")

    def test_an_unparsable_row_is_reported_never_failed(self, capsys: pytest.CaptureFixture[str]) -> None:
        ok, out = _check(["teatree-worker\tlots\t0\tsoon\trunning", "short-row"], capsys)
        assert ok
        assert out.count("INFO") == 2

    def test_an_unreachable_daemon_is_an_info_naming_the_venue_that_can_read_it(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ok, out = _check(None, capsys)
        assert ok
        assert out.startswith("INFO")
        assert "teatree-worker" in out


class TestTheWorkerGateGroupCountsIt:
    def test_a_restart_loop_reddens_the_worker_gates(self) -> None:
        with (
            patch.object(run_checks, "_check_worker_running", return_value=True),
            patch.object(run_checks, "_check_worker_singleton_holder", return_value=True),
            patch.object(run_checks, "_check_worker_skills_present", return_value=True),
            patch.object(run_checks, "_check_worker_memory_cap", return_value=True),
            patch.object(run_checks, "_check_resume_ceiling_reachable", return_value=True),
            patch.object(run_checks, "_check_clean_exit_restart_loop", return_value=False),
        ):
            assert run_checks._run_worker_gates() is False


def _done(stdout: str, returncode: int = 0) -> CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


class TestReadingTheDaemon:
    def test_no_docker_binary_reads_as_unreachable(self) -> None:
        with patch.object(checks_runtime, "run_allowed_to_fail", side_effect=FileNotFoundError("docker")):
            assert _inspect_compose_containers() is None

    def test_a_refused_socket_reads_as_unreachable(self) -> None:
        with patch.object(checks_runtime, "run_allowed_to_fail", return_value=_done("", returncode=1)):
            assert _inspect_compose_containers() is None

    def test_a_daemon_running_no_compose_container_answers_empty(self) -> None:
        with patch.object(checks_runtime, "run_allowed_to_fail", return_value=_done("")):
            assert _inspect_compose_containers() == []

    def test_each_compose_container_answers_one_row(self) -> None:
        rows = f"{_row('teatree-worker', 3, 0, 'x', 'running')}\n{_row('teatree-admin', 0, 0, 'y', 'running')}\n"
        with patch.object(checks_runtime, "run_allowed_to_fail", side_effect=[_done("abc\ndef\n"), _done(rows)]) as run:
            assert _inspect_compose_containers() == rows.splitlines()
        assert run.call_args_list[1].args[0][-2:] == ["abc", "def"]
