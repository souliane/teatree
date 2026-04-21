"""Tests for teatree.core.apps — CoreConfig.ready() and helper functions."""

import subprocess
import sys
from unittest.mock import MagicMock

import pytest
from django.apps import apps
from django.test import TestCase, override_settings

from teatree.core.apps import _cleanup_workers, _start_periodic_sync, _start_workers, _worker_processes


def _get_core_config():
    """Get the already-registered CoreConfig instance."""
    return apps.get_app_config("core")


class TestCoreConfigReady:
    def test_does_nothing_when_run_main_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ready() should not start workers/sync when RUN_MAIN != 'true'."""
        monkeypatch.delenv("RUN_MAIN", raising=False)
        monkeypatch.delenv("_TEETREE_WORKER", raising=False)

        started: list[str] = []
        monkeypatch.setattr("teatree.core.apps._start_periodic_sync", lambda: started.append("sync"))
        monkeypatch.setattr("teatree.core.apps._start_workers", lambda: started.append("workers"))

        config = _get_core_config()
        config.ready()

        assert started == []

    def test_does_nothing_for_worker_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ready() should not start workers when _TEETREE_WORKER is set."""
        monkeypatch.setenv("RUN_MAIN", "true")
        monkeypatch.setenv("_TEETREE_WORKER", "1")

        started: list[str] = []
        monkeypatch.setattr("teatree.core.apps._start_periodic_sync", lambda: started.append("sync"))
        monkeypatch.setattr("teatree.core.apps._start_workers", lambda: started.append("workers"))

        original_argv = sys.argv
        sys.argv = ["manage.py", "runserver"]
        try:
            config = _get_core_config()
            config.ready()
        finally:
            sys.argv = original_argv

        assert started == []

    def test_does_nothing_for_non_runserver_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ready() should not start workers for non-runserver commands like 'migrate'."""
        monkeypatch.setenv("RUN_MAIN", "true")
        monkeypatch.delenv("_TEETREE_WORKER", raising=False)

        started: list[str] = []
        monkeypatch.setattr("teatree.core.apps._start_periodic_sync", lambda: started.append("sync"))
        monkeypatch.setattr("teatree.core.apps._start_workers", lambda: started.append("workers"))

        original_argv = sys.argv
        sys.argv = ["manage.py", "migrate"]
        try:
            config = _get_core_config()
            config.ready()
        finally:
            sys.argv = original_argv

        assert started == []

    def test_starts_sync_and_workers_for_runserver(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ready() starts periodic sync and workers when RUN_MAIN=true and runserver."""
        monkeypatch.setenv("RUN_MAIN", "true")
        monkeypatch.delenv("_TEETREE_WORKER", raising=False)

        started: list[str] = []
        monkeypatch.setattr("teatree.core.apps._start_periodic_sync", lambda: started.append("sync"))
        monkeypatch.setattr("teatree.core.apps._start_workers", lambda: started.append("workers"))

        original_argv = sys.argv
        sys.argv = ["manage.py", "runserver"]
        try:
            config = _get_core_config()
            config.ready()
        finally:
            sys.argv = original_argv

        assert started == ["sync", "workers"]


class TestStartPeriodicSync(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch

    def test_starts_daemon_thread(self) -> None:
        """_start_periodic_sync creates a daemon thread named 'teatree-periodic-sync'."""
        import threading  # noqa: PLC0415

        captured_targets: list[object] = []
        captured_names: list[str] = []

        original_init = threading.Thread.__init__

        def _tracking_init(self_thread: threading.Thread, *args: object, **kwargs: object) -> None:
            original_init(self_thread, *args, **kwargs)
            captured_names.append(self_thread.name)
            captured_targets.append(kwargs.get("target"))

        def _mock_start(self_thread: threading.Thread) -> None:
            pass

        self._monkeypatch.setattr(threading.Thread, "__init__", _tracking_init)
        self._monkeypatch.setattr(threading.Thread, "start", _mock_start)

        _start_periodic_sync()

        assert "teatree-periodic-sync" in captured_names

    @override_settings(
        TASKS={
            "default": {
                "BACKEND": "django_tasks.backends.immediate.ImmediateBackend",
            },
        },
    )
    def test_loop_enqueues_and_handles_exception(self) -> None:
        """Exercise the _loop function inside _start_periodic_sync (success + exception paths)."""
        import threading  # noqa: PLC0415

        captured_target = None

        original_init = threading.Thread.__init__

        def _capture_init(self_thread: threading.Thread, *args: object, **kwargs: object) -> None:
            nonlocal captured_target
            original_init(self_thread, *args, **kwargs)
            captured_target = kwargs.get("target")

        def _mock_start(self_thread: threading.Thread) -> None:
            pass

        self._monkeypatch.setattr(threading.Thread, "__init__", _capture_init)
        self._monkeypatch.setattr(threading.Thread, "start", _mock_start)

        # Make the wait() return immediately and break after 2 iterations
        call_count = 0

        def _fast_wait(self_event: threading.Event, timeout: float | None = None) -> bool:
            nonlocal call_count
            call_count += 1
            if call_count > 2:
                msg = "stop loop"
                raise StopIteration(msg)
            return True

        self._monkeypatch.setattr(threading.Event, "wait", _fast_wait)

        _start_periodic_sync()

        assert captured_target is not None
        # The loop will run twice: first iteration succeeds (enqueue), second raises StopIteration
        with pytest.raises(StopIteration):
            captured_target()

        assert call_count == 3  # 2 successful + 1 that raised

    @override_settings(
        TASKS={
            "default": {
                "BACKEND": "django_tasks.backends.immediate.ImmediateBackend",
            },
        },
    )
    def test_loop_handles_enqueue_exception(self) -> None:
        """Exercise the exception branch inside the _loop function."""
        import threading  # noqa: PLC0415

        captured_target = None

        original_init = threading.Thread.__init__

        def _capture_init(self_thread: threading.Thread, *args: object, **kwargs: object) -> None:
            nonlocal captured_target
            original_init(self_thread, *args, **kwargs)
            captured_target = kwargs.get("target")

        def _mock_start(self_thread: threading.Thread) -> None:
            pass

        self._monkeypatch.setattr(threading.Thread, "__init__", _capture_init)
        self._monkeypatch.setattr(threading.Thread, "start", _mock_start)

        call_count = 0

        def _fast_wait(self_event: threading.Event, timeout: float | None = None) -> bool:
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                msg = "stop loop"
                raise StopIteration(msg)
            return True

        self._monkeypatch.setattr(threading.Event, "wait", _fast_wait)

        # Make the task module's drain_headless_queue raise when enqueued
        # (drain runs every tick; sync_followup only runs every 30th tick)
        import teatree.core.tasks as tasks_mod  # noqa: PLC0415

        mock_drain = MagicMock()
        mock_drain.enqueue.side_effect = RuntimeError("enqueue failed")
        self._monkeypatch.setattr(tasks_mod, "drain_headless_queue", mock_drain)

        _start_periodic_sync()

        assert captured_target is not None
        with pytest.raises(StopIteration):
            captured_target()

        mock_drain.enqueue.assert_called_once()


class TestStartWorkers:
    def test_spawns_processes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
        """_start_workers spawns the configured number of worker processes."""
        from django.conf import settings  # noqa: PLC0415

        monkeypatch.setattr(settings, "TEATREE_WORKER_COUNT", 2, raising=False)
        monkeypatch.setattr(settings, "BASE_DIR", tmp_path, raising=False)
        (tmp_path / "manage.py").touch()

        mock_popen = MagicMock()
        mock_popen.return_value.pid = 12345
        monkeypatch.setattr("teatree.utils.run.subprocess.Popen", mock_popen)

        # Clear the global list before test
        _worker_processes.clear()

        _start_workers()

        assert mock_popen.call_count == 2
        assert len(_worker_processes) == 2

        # Clean up
        _worker_processes.clear()


class TestCleanupWorkers:
    def test_terminates_and_waits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_cleanup_workers terminates processes, waits, then clears the list."""
        mock_proc1 = MagicMock()
        mock_proc2 = MagicMock()
        mock_proc2.wait.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=5)

        _worker_processes.clear()
        _worker_processes.extend([mock_proc1, mock_proc2])

        _cleanup_workers()

        mock_proc1.terminate.assert_called_once()
        mock_proc2.terminate.assert_called_once()
        mock_proc1.wait.assert_called_once_with(timeout=5)
        mock_proc2.wait.assert_called_once_with(timeout=5)
        mock_proc2.kill.assert_called_once()
        assert _worker_processes == []
