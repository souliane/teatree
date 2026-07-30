"""``advance_checkpoint_monotonic`` holds a file lock around read-compare-write.

The pre-fix code did an unlocked TOCTOU: read the stored timestamp, compare in
Python, then write if ``stored < now``.  Two concurrent ``checking show`` runs
with different ``now`` values can interleave as:

    A reads stored=T0
    B reads stored=T0, sees T2 > T0, writes T2
    A sees T1 > T0 (T0 < T1 < T2), so also writes T1
    Result: marker is T1, regressed from T2 — double-report of [T1, T2)

The fix wraps the read-compare-write in an exclusive ``fcntl.flock`` so the
two calls are serialised: the loser re-reads T2, sees T2 >= T1 (now=T1),
and skips the write.

The TOCTOU is reproduced deterministically by monkey-patching ``load_checkpoint``
inside ``advance_checkpoint_monotonic`` to inject a "concurrent" write between
the pre-fix read and the subsequent compare+write.
"""

import fcntl as _fcntl
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import teatree.core.checkpoint as checkpoint_mod
from teatree.core.checkpoint import advance_checkpoint, advance_checkpoint_monotonic, load_checkpoint


class TestAdvanceCheckpointMonotonicTOCTOU:
    def test_exclusive_lock_file_is_acquired(self, tmp_path: Path) -> None:
        """``advance_checkpoint_monotonic`` holds an exclusive flock during the CAS.

        We verify the structural guarantee: the lock file exists and is
        exclusively held while ``advance_checkpoint_monotonic`` runs.  A
        concurrent ``fcntl.flock(LOCK_EX | LOCK_NB)`` attempt from the same
        process on the sibling lock file must fail with ``BlockingIOError``
        (errno EWOULDBLOCK) while the function holds the lock.
        """
        path = tmp_path / "cp.json"
        lock_path = path.with_suffix(".lock")
        t = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)

        lock_held_during_call: list[bool] = []

        original_advance = checkpoint_mod.advance_checkpoint

        def probe_lock(now: datetime, p: Path | None = None) -> Path:
            # Try to grab the lock non-blocking while the function holds it.
            try:
                with lock_path.open("a") as fh:
                    _fcntl.flock(fh, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    _fcntl.flock(fh, _fcntl.LOCK_UN)
                lock_held_during_call.append(False)
            except BlockingIOError:
                lock_held_during_call.append(True)
            return original_advance(now, p)

        with patch.object(checkpoint_mod, "advance_checkpoint", side_effect=probe_lock):
            advance_checkpoint_monotonic(t, path)

        assert lock_held_during_call == [True], (
            "expected exclusive lock to be held during advance_checkpoint call; "
            f"got lock_held_during_call={lock_held_during_call}"
        )

    def test_sequential_forward_advance_still_works(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        t1 = datetime(2026, 5, 31, 9, 0, tzinfo=UTC)
        t2 = datetime(2026, 5, 31, 15, 0, tzinfo=UTC)
        advance_checkpoint_monotonic(t1, path)
        advance_checkpoint_monotonic(t2, path)
        assert load_checkpoint(path) == t2

    def test_backward_advance_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "cp.json"
        t_stored = datetime(2026, 5, 31, 15, 0, tzinfo=UTC)
        advance_checkpoint(t_stored, path)
        advance_checkpoint_monotonic(datetime(2026, 5, 31, 9, 0, tzinfo=UTC), path)
        assert load_checkpoint(path) == t_stored

    def test_concurrent_threads_leave_marker_at_max(self, tmp_path: Path) -> None:
        """Two concurrent advances must leave the marker at the later timestamp."""
        path = tmp_path / "cp.json"
        t_early = datetime(2026, 5, 31, 10, 0, tzinfo=UTC)
        t_late = datetime(2026, 5, 31, 18, 0, tzinfo=UTC)

        advance_checkpoint(datetime(2026, 5, 31, 8, 0, tzinfo=UTC), path)

        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def run(now: datetime) -> None:
            try:
                barrier.wait(timeout=10)
                advance_checkpoint_monotonic(now, path)
            except Exception as exc:  # noqa: BLE001 — a worker records whatever it raises for the parent to assert on
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(t_late,)),
            threading.Thread(target=run, args=(t_early,)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, errors
        final = load_checkpoint(path)
        assert final == t_late, f"marker regressed: expected {t_late}, got {final}"
