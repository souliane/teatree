"""manage.py worker — the singleton loop-timer worker command (#1796).

The command wires the flock singleton + SIGTERM/SIGINT handlers around the
:class:`teatree.loops.worker.LoopWorker`; a second invocation while one holds the
flock refuses with a non-zero exit. Collaborators are stubbed so the test never
spawns a real, forever-blocking worker or mutates the process's signal handlers.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.management import call_command

from teatree.utils.singleton import AlreadyRunningError


def test_runs_the_worker_under_the_singleton_and_installs_signals() -> None:
    with (
        patch("teatree.loops.worker.LoopWorker") as worker_cls,
        patch("teatree.core.management.commands.worker.signal.signal") as signal_signal,
    ):
        call_command("worker")
    worker_cls.return_value.run.assert_called_once_with()
    assert signal_signal.call_count == 2  # SIGTERM + SIGINT


def test_second_instance_refuses_with_nonzero_exit() -> None:
    def _raise(_name: str) -> None:
        name = "worker"
        raise AlreadyRunningError(name, 4321, Path(name).with_suffix(".pid"))

    with (
        patch("teatree.utils.singleton.singleton", side_effect=_raise),
        pytest.raises(SystemExit) as exc,
    ):
        call_command("worker")
    assert exc.value.code == 1
