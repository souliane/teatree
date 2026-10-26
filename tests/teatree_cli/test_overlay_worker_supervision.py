"""``t3 <overlay> worker`` reports the children's real fate, not just that it spawned them.

The supervisor waited on every ``db_worker`` child and discarded each exit code, so a
worker that died on startup (unmigrated DB, bad settings) left the operator with
"Started 3 worker(s)" and exit 0 while nothing drained the queue.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from teatree.cli.overlay import OverlayAppBuilder, _run_workers
from teatree.utils.singleton import WORKER_SINGLETON, flock_is_held


def _processes(*codes: int) -> list[MagicMock]:
    return [MagicMock(**{"wait.return_value": code}) for code in codes]


class TestWorkerExitCodesAreCounted:
    @pytest.mark.parametrize(
        ("codes", "expected"),
        [((0, 0, 0), 0), ((1, 0, 0), 1), ((1, 2, 3), 3)],
    )
    def test_the_failure_count_is_returned(self, tmp_path: Path, codes: tuple[int, ...], expected: int) -> None:
        with patch("teatree.cli.overlay.spawn", side_effect=_processes(*codes)):
            assert _run_workers(tmp_path, "acme", len(codes), 1.0) == expected

    def test_a_ctrl_c_shutdown_counts_no_failures(self, tmp_path: Path) -> None:
        # The interrupt lands on the supervising wait; the post-terminate wait then returns.
        process = MagicMock(**{"wait.side_effect": [KeyboardInterrupt, -15]})
        with patch("teatree.cli.overlay.spawn", return_value=process):
            assert _run_workers(tmp_path, "acme", 1, 1.0) == 0
        process.terminate.assert_called_once()


def test_overlay_worker_holds_the_control_database_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "control" / "db.sqlite3"
    monkeypatch.setenv("T3_CONFIG_DB", str(database))
    lock = database.with_name(f"{database.name}.worker.pid")
    app = OverlayAppBuilder(overlay_name="acme", project_path=tmp_path).build()

    def check_running(_project: Path, _name: str, _count: int, _interval: float) -> int:
        assert flock_is_held(WORKER_SINGLETON, pid_path=lock)
        return 0

    with (
        patch("teatree.cli.overlay.project_env_is_drivable", return_value=True),
        patch("teatree.cli.overlay._run_workers", side_effect=check_running),
    ):
        result = CliRunner().invoke(app, ["worker", "--count", "1"])
    assert result.exit_code == 0
    assert not flock_is_held(WORKER_SINGLETON, pid_path=lock)
