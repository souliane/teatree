"""Orphaned ``t3 mcp serve`` processes are reaped; a live client's server never is.

Integration-leaning: the snapshot/reap path is proven against a REAL orphaned
process (double-spawned so the OS reparents it to PID 1) under a test-scoped
matcher, so the ``ps`` parsing, the PPID-1 classification, and the SIGTERM all
run for real. Pure token-matching logic is covered by parametrized unit tests.
"""

import os
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import teatree.mcp.serve_lifecycle as serve_lifecycle_mod
from teatree.cli.mcp import serve
from teatree.mcp.serve_lifecycle import (
    ProcessRecord,
    _hard_exit,
    is_serve_command,
    orphan_detection_applies,
    orphaned_serve_pids,
    process_snapshot,
    reap_orphaned_servers,
    start_parent_death_watch,
    watch_until_orphaned,
)
from teatree.utils.run import CommandFailedError

_SERVE_COMMAND = (
    "/Users/x/.local/share/uv/tools/teatree/bin/python "  # privacy-scan:allow
    "/Users/x/.local/bin/t3 mcp serve"  # privacy-scan:allow
)


class TestIsServeCommand:
    @pytest.mark.parametrize(
        "command",
        [
            _SERVE_COMMAND,
            "t3 mcp serve",
            "/home/x/.local/bin/t3 mcp serve",  # privacy-scan:allow
            "python3.13 /usr/local/bin/t3 mcp serve",
        ],
    )
    def test_matches_real_serve_invocations(self, command: str) -> None:
        assert is_serve_command(command)

    @pytest.mark.parametrize(
        "command",
        [
            "grep t3 mcp serve",
            "vim notes/t3 mcp serve.md",
            "t3 mcp reconnect",
            "t3 loop serve",
            "/usr/bin/python -m http.server",
            "",
        ],
    )
    def test_rejects_lookalikes(self, command: str) -> None:
        assert not is_serve_command(command)


class TestOrphanedServePids:
    def test_selects_only_ppid1_serve_rows_excluding_self(self) -> None:
        records = [
            ProcessRecord(pid=10, ppid=1, command=_SERVE_COMMAND),
            ProcessRecord(pid=11, ppid=500, command=_SERVE_COMMAND),  # live client
            ProcessRecord(pid=12, ppid=1, command="/usr/sbin/distnoted"),
            ProcessRecord(pid=13, ppid=1, command=_SERVE_COMMAND),  # ourselves
        ]

        assert orphaned_serve_pids(records, self_pid=13) == [10]


def _spawn_real_orphan(marker: str) -> int:
    """Start a ``sleep`` re-parented to PID 1 (its intermediate parent exits)."""
    # The child's fds are redirected so it does not inherit the stdout pipe —
    # otherwise ``subprocess.run`` would block on EOF until the sleep ends.
    out = subprocess.run(
        ["/bin/sh", "-c", f"sleep {marker} >/dev/null 2>&1 & echo $!"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return int(out.strip())


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _reaped_gone(pid: int, marker: str) -> bool:
    """Whether the SIGTERMed orphan is no longer a live matching process.

    The orphan's parent is PID 1, so this process cannot ``waitpid`` its zombie —
    init reaps it, after which ``os.kill(pid, 0)`` raises ProcessLookupError. In the
    brief window before init collects it the pid can linger as a zombie: ``os.kill``
    still succeeds, but a defunct process no longer runs the marked ``sleep`` command,
    so it drops out of the ps snapshot's live matching rows. Either state proves the
    reap landed; a still-running orphan keeps BOTH ``_alive`` and the snapshot match.
    """
    if not _alive(pid):
        return True
    return not any(record.pid == pid and marker in record.command for record in process_snapshot())


@pytest.fixture
def _on_a_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the HOST precondition the reap/watch cases describe.

    ``orphan_detection_applies`` is False in a container and BOTH entry points return
    early there — and the suite itself runs containerized in CI. Without this the cases
    below would go green by short-circuiting rather than by exercising the PID-1 proof
    they name, and the one that wants a real thread back would get ``None``.
    """
    monkeypatch.setattr(serve_lifecycle_mod, "is_running_in_container", lambda: False)


@pytest.mark.usefixtures("_on_a_host")
class TestReapRealOrphan:
    def test_reaps_a_real_reparented_process_and_only_it(self) -> None:
        # Unique per test process so a leftover orphan from an aborted earlier
        # run can never satisfy (or pollute) this run's matcher.
        marker = f"31415.{os.getpid()}"
        pid = _spawn_real_orphan(marker)
        try:
            assert _wait_for(
                lambda: any(r.pid == pid and r.ppid == 1 for r in process_snapshot()),
            ), "orphan never appeared reparented to PID 1 in the ps snapshot"

            reaped = reap_orphaned_servers(matcher=lambda command: marker in command)

            assert reaped == [pid]
            # A loaded CI runner can lag the SIGTERM delivery / init's zombie reap well
            # past the default 5s window, so allow longer and accept a defunct (zombie)
            # orphan as reaped — the process is no longer running the marked command.
            assert _wait_for(lambda: _reaped_gone(pid, marker), timeout=20.0), "reaped orphan is still alive"
        finally:
            if _alive(pid):
                os.kill(pid, 15)

    def test_reaps_nothing_when_no_command_matches(self) -> None:
        assert reap_orphaned_servers(matcher=lambda command: "no-such-marker-9e9e9" in command) == []


class TestWatchUntilOrphaned:
    def test_fires_only_once_reparented_to_init(self) -> None:
        ppids = iter([4242, 4242, 1])
        fired = []

        watch_until_orphaned(
            get_ppid=lambda: next(ppids),
            on_orphaned=lambda: fired.append(True),
            poll_seconds=0.001,
        )

        assert fired == [True]


class TestProcessSnapshotFailsOpen:
    def test_returns_empty_on_a_ps_failure(self) -> None:
        with patch.object(serve_lifecycle_mod, "run_allowed_to_fail", side_effect=CommandFailedError("ps", 1, "", "")):
            assert process_snapshot() == []

    def test_returns_empty_on_an_os_error(self) -> None:
        with patch.object(serve_lifecycle_mod, "run_allowed_to_fail", side_effect=OSError("no ps")):
            assert process_snapshot() == []

    def test_skips_malformed_rows(self) -> None:
        out = "10 1 /usr/bin/t3 mcp serve\ngarbage line without numeric ids\n20 x badppid\n"
        with patch.object(serve_lifecycle_mod, "run_allowed_to_fail", return_value=SimpleNamespace(stdout=out)):
            records = process_snapshot()
        assert records == [ProcessRecord(pid=10, ppid=1, command="/usr/bin/t3 mcp serve")]


class TestReapSkipsUnsignalablePids:
    def test_a_pid_that_vanishes_is_skipped(self) -> None:
        records = [ProcessRecord(pid=999, ppid=1, command="t3 mcp serve")]
        with (
            patch.object(serve_lifecycle_mod, "process_snapshot", return_value=records),
            patch.object(serve_lifecycle_mod.os, "getpid", return_value=1),
            patch.object(serve_lifecycle_mod.os, "kill", side_effect=ProcessLookupError),
        ):
            assert reap_orphaned_servers() == []


class TestHardExit:
    def test_hard_exits_the_process(self) -> None:
        with patch.object(serve_lifecycle_mod.os, "_exit") as hard:
            _hard_exit()
        hard.assert_called_once_with(0)


@pytest.mark.usefixtures("_on_a_host")
class TestStartParentDeathWatch:
    def test_starts_a_named_daemon_thread_running_the_watch(self) -> None:
        # patch the watch loop to a no-op so the started thread exits immediately
        with patch("teatree.mcp.serve_lifecycle.watch_until_orphaned") as watch:
            thread = start_parent_death_watch(poll_seconds=0.001)
            assert thread is not None
            thread.join(timeout=2.0)

        assert thread.daemon is True
        assert thread.name == "mcp-serve-parent-death-watch"
        assert not thread.is_alive()
        watch.assert_called_once()
        assert watch.call_args.kwargs["poll_seconds"] == pytest.approx(0.001)


class TestServeWiring:
    def test_serve_reaps_then_arms_watchdog_before_blocking(self) -> None:
        with (
            patch("teatree.cli.mcp.reap_orphaned_servers") as reap,
            patch("teatree.cli.mcp.start_parent_death_watch") as watch,
            patch("teatree.cli.mcp.ensure_django"),
            patch("teatree.mcp.server.build_server") as build,
        ):
            serve()

        reap.assert_called_once_with()
        watch.assert_called_once_with()
        build.return_value.run.assert_called_once_with("stdio")


class TestOrphanDetectionIsHostOnly:
    """PID 1 proves the client is gone on a host; in a container it is the entrypoint.

    A server started by ``docker compose run --entrypoint t3 … mcp serve`` has
    ``getppid() == 1`` from its first instant, so the watchdog hard-exited it before it
    answered a single request (exit 0, empty stdout) and the reaper would SIGTERM every
    sibling server in the same container. That is what blocked routing the MCP server
    into the containerized stack.
    """

    def test_the_pid1_proof_holds_on_a_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEATREE_ROLE", raising=False)

        assert orphan_detection_applies(containerized=False) is True

    def test_the_pid1_proof_does_not_hold_in_a_container(self) -> None:
        assert orphan_detection_applies(containerized=True) is False

    def test_the_containerized_runtime_is_detected_by_its_role(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The deploy entrypoint sets ``TEATREE_ROLE`` for every role."""
        monkeypatch.setenv("TEATREE_ROLE", "worker")

        assert orphan_detection_applies() is False

    def test_no_watchdog_thread_is_armed_in_a_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_ROLE", "worker")

        assert start_parent_death_watch() is None

    def test_no_sibling_is_reaped_in_a_container(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEATREE_ROLE", "worker")

        with patch.object(serve_lifecycle_mod, "os") as never_signalled:
            assert reap_orphaned_servers(matcher=lambda _: True) == []

        never_signalled.kill.assert_not_called()
