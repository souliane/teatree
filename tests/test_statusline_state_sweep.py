"""Stale statusline-state file sweep (#130).

The hook writes per-session state files into ``/tmp/claude-statusline/``
(``<session>.skills``, ``.agents``, ``.crons`` …). Sessions end without
cleaning them up, so the directory accumulates 100+ stale files over
time. A throttled mtime sweep on the state-write path removes files older
than the retention window so the directory stays bounded.
"""

import os
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.scripts.hook_router as router
from hooks.scripts.hook_router import _STATE_FILE_MAX_AGE_SECONDS, _ensure_state_dir, _sweep_stale_state_files


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path):
    original = router.STATE_DIR
    router.STATE_DIR = tmp_path / "state"
    router.STATE_DIR.mkdir(parents=True, exist_ok=True)
    yield
    router.STATE_DIR = original


def _touch(name: str, *, age_seconds: float) -> Path:
    path = router.STATE_DIR / name
    path.write_text("x", encoding="utf-8")
    mtime = time.time() - age_seconds
    os.utime(path, (mtime, mtime))
    return path


class TestSweepStaleStateFiles:
    def test_removes_files_older_than_retention(self) -> None:
        stale = _touch("old-session.skills", age_seconds=_STATE_FILE_MAX_AGE_SECONDS + 3600)
        _sweep_stale_state_files()
        assert not stale.exists()

    def test_keeps_recent_files(self) -> None:
        fresh = _touch("live-session.skills", age_seconds=60)
        _sweep_stale_state_files()
        assert fresh.exists()

    def test_keeps_file_exactly_at_boundary(self) -> None:
        boundary = _touch("boundary.skills", age_seconds=_STATE_FILE_MAX_AGE_SECONDS - 60)
        _sweep_stale_state_files()
        assert boundary.exists()

    def test_sweep_does_not_remove_the_throttle_sentinel_itself(self) -> None:
        # The sentinel is refreshed by the sweep, so it must survive even
        # though a sweep just ran.
        _sweep_stale_state_files()
        sentinel = router.STATE_DIR / router._SWEEP_SENTINEL
        assert sentinel.exists()

    def test_missing_dir_is_a_noop(self) -> None:
        shutil.rmtree(router.STATE_DIR)
        # Must not raise.
        _sweep_stale_state_files()

    def test_throttled_so_not_every_call_walks_the_dir(self) -> None:
        # First sweep runs and writes the sentinel; a second immediate call
        # is throttled (sentinel is fresh) and does NOT delete a file that
        # only just aged past the window between the two calls.
        _sweep_stale_state_files()
        stale = _touch("aged.skills", age_seconds=_STATE_FILE_MAX_AGE_SECONDS + 3600)
        _sweep_stale_state_files()
        assert stale.exists(), "second call within the throttle window should skip the walk"


class TestEnsureStateDirRunsSweep:
    def test_ensure_state_dir_invokes_sweep(self) -> None:
        with patch.object(router, "_sweep_stale_state_files") as sweep:
            _ensure_state_dir()
        sweep.assert_called_once()

    def test_ensure_state_dir_survives_sweep_failure(self) -> None:
        # The sweep is best-effort: a failure must never crash a state write.
        with patch.object(router, "_sweep_stale_state_files", side_effect=OSError("boom")):
            _ensure_state_dir()
        assert router.STATE_DIR.is_dir()
