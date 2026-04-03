"""Tests for teatree.agents.process_registry — in-memory subprocess tracking."""

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest

from teatree.agents.process_registry import (
    ProcessEntry,
    _registry,
    cleanup_exited,
    list_processes,
    register,
    terminate_all,
    unregister,
)


def _make_mock_process(pid: int, *, alive: bool = True) -> MagicMock:
    """Create a MagicMock mimicking subprocess.Popen with the given pid/alive state."""
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None if alive else 0
    return proc


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    """Ensure the module-level registry is empty before and after each test."""
    _registry.clear()
    yield
    _registry.clear()


class TestProcessEntryAlive:
    """Cover ProcessEntry.alive property (line 25)."""

    def test_alive_when_poll_returns_none(self) -> None:
        proc = _make_mock_process(1, alive=True)
        entry = ProcessEntry(process=proc, description="test")

        assert entry.alive is True
        proc.poll.assert_called_once()

    def test_dead_when_poll_returns_exit_code(self) -> None:
        proc = _make_mock_process(2, alive=False)
        entry = ProcessEntry(process=proc, description="test")

        assert entry.alive is False
        proc.poll.assert_called_once()


class TestUnregister:
    """Cover unregister() (line 39)."""

    def test_removes_registered_process(self) -> None:
        proc = _make_mock_process(10)
        register(proc, "worker")

        assert 10 in _registry
        unregister(10)
        assert 10 not in _registry

    def test_noop_for_unknown_pid(self) -> None:
        unregister(9999)
        assert _registry == {}


class TestCleanupExited:
    """Cover cleanup_exited() (lines 43-46)."""

    def test_removes_dead_processes_returns_count(self) -> None:
        alive_proc = _make_mock_process(1, alive=True)
        dead_proc = _make_mock_process(2, alive=False)
        register(alive_proc, "alive")
        register(dead_proc, "dead")

        removed = cleanup_exited()

        assert removed == 1
        assert 1 in _registry
        assert 2 not in _registry

    def test_returns_zero_when_all_alive(self) -> None:
        register(_make_mock_process(1, alive=True), "a")
        register(_make_mock_process(2, alive=True), "b")

        assert cleanup_exited() == 0
        assert len(_registry) == 2

    def test_returns_zero_on_empty_registry(self) -> None:
        assert cleanup_exited() == 0


class TestTerminateAll:
    """Cover terminate_all() (lines 50-57)."""

    def test_terminates_alive_processes_and_clears_registry(self) -> None:
        alive_proc = _make_mock_process(1, alive=True)
        dead_proc = _make_mock_process(2, alive=False)
        register(alive_proc, "alive-worker")
        register(dead_proc, "dead-worker")

        count = terminate_all()

        assert count == 1
        alive_proc.terminate.assert_called_once()
        dead_proc.terminate.assert_not_called()
        assert _registry == {}

    def test_returns_zero_on_empty_registry(self) -> None:
        assert terminate_all() == 0
        assert _registry == {}


class TestListProcesses:
    """Cover list_processes() (lines 61-62)."""

    def test_returns_info_dicts_for_alive_processes(self) -> None:
        proc = _make_mock_process(42, alive=True)
        register(proc, "my-server")

        result = list_processes()

        assert len(result) == 1
        entry = result[0]
        assert entry["pid"] == 42
        assert entry["description"] == "my-server"
        assert entry["alive"] is True
        assert isinstance(entry["uptime_seconds"], int)

    def test_cleans_up_dead_before_listing(self) -> None:
        dead_proc = _make_mock_process(7, alive=False)
        register(dead_proc, "gone")

        result = list_processes()

        assert result == []
        assert 7 not in _registry

    def test_returns_empty_list_when_no_processes(self) -> None:
        assert list_processes() == []
