import asyncio
import os
import signal
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import PropertyMock, patch

from teatree.utils.process_group import stop_process_group
from tests._process_liveness import HOLDS_THE_PIPES_FROM_ANOTHER_SESSION, is_gone_within, written_pids

_IGNORES_TERM = "trap '' TERM; echo $$ > leader; sleep 30 & echo $! > child; wait"
_STOP_BUDGET_SECONDS = 1.5


async def _start(script: str, directory: Path) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        script,
        cwd=directory,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )


def _kill_what_is_left(process: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


class TestStopProcessGroup:
    def test_a_command_that_ignores_term_dies_with_its_children(self, tmp_path: Path) -> None:
        async def run() -> tuple[int | None, list[int]]:
            process = await _start(_IGNORES_TERM, tmp_path)
            try:
                pids = await written_pids(tmp_path, "leader", "child")
                await asyncio.wait_for(stop_process_group(process), timeout=5)
            finally:
                _kill_what_is_left(process)
            return process.returncode, pids

        returncode, pids = asyncio.run(run())

        assert returncode == -signal.SIGKILL
        assert all(is_gone_within(pid, seconds=3) for pid in pids)

    def test_stopping_a_group_that_is_already_gone_is_harmless(self, tmp_path: Path) -> None:
        async def run() -> int | None:
            process = await _start("sleep 30", tmp_path)
            try:
                await stop_process_group(process)
                await stop_process_group(process)
            finally:
                _kill_what_is_left(process)
            return process.returncode

        assert asyncio.run(run()) == -signal.SIGKILL

    def test_a_descendant_that_left_the_session_and_holds_the_pipes_does_not_delay_the_stop(
        self, tmp_path: Path
    ) -> None:
        async def run() -> tuple[float, int | None, int]:
            process = await _start(HOLDS_THE_PIPES_FROM_ANOTHER_SESSION, tmp_path)
            (escapee,) = await written_pids(tmp_path, "escapee")
            try:
                started = time.monotonic()
                await stop_process_group(process)
                elapsed = time.monotonic() - started
                os.kill(escapee, 0)
            finally:
                _kill_what_is_left(process)
                with suppress(ProcessLookupError):
                    os.kill(escapee, signal.SIGKILL)
                await asyncio.wait_for(process.communicate(), timeout=5)
            return elapsed, process.returncode, escapee

        elapsed, returncode, _escapee = asyncio.run(run())

        assert elapsed < _STOP_BUDGET_SECONDS
        assert returncode == -signal.SIGKILL

    def test_a_group_the_host_refuses_to_signal_is_killed_through_its_leader(self, tmp_path: Path) -> None:
        async def run() -> int | None:
            process = await _start("exec sleep 30", tmp_path)
            try:
                with patch("os.killpg", side_effect=PermissionError):
                    await stop_process_group(process)
            finally:
                _kill_what_is_left(process)
            return process.returncode

        assert asyncio.run(run()) == -signal.SIGKILL

    def test_a_refused_signal_to_a_leader_that_already_exited_is_harmless(self, tmp_path: Path) -> None:
        async def run() -> None:
            process = await _start("true", tmp_path)
            await process.wait()
            await asyncio.sleep(0.05)
            with patch("os.killpg", side_effect=PermissionError):
                await stop_process_group(process)

        asyncio.run(run())

    def test_the_stop_returns_only_once_the_leader_has_an_exit_status(self, tmp_path: Path) -> None:
        reads: list[None] = []

        def exit_status() -> int | None:
            reads.append(None)
            return None if len(reads) <= 3 else -signal.SIGKILL

        async def run() -> None:
            process = await _start("sleep 30", tmp_path)
            try:
                with patch.object(asyncio.subprocess.Process, "returncode", new_callable=PropertyMock) as returncode:
                    returncode.side_effect = exit_status
                    await stop_process_group(process)
            finally:
                _kill_what_is_left(process)

        asyncio.run(run())

        assert len(reads) == 4
