"""Cross-process speaker mutual exclusion around ``say`` (#2152).

Two local reads — whether two in-process daemon threads or two separate
detached ``t3 speak`` subprocesses — must never run ``say`` at the same time,
or messages talk over each other. :func:`_speak_local` wraps the actual ``say``
call in a single cross-process ``fcntl.flock`` on a lockfile under the teatree
state dir, so plays are mutually exclusive machine-wide when the lock is
available. The lock is best-effort: if the wait budget elapses the read falls
through and plays anyway — a brief overlap is better than a silenced read.

Observability: a fake ``say`` script (on PATH ahead of the real one) appends
``START <ns>`` / ``STOP <ns>`` to a log with a sleep between, so two concurrent
plays that overlapped would interleave (a second START before the first STOP).
The lock makes the second START land at-or-after the first STOP. The fake is a
real subprocess (the lock must serialize real processes), not a mock — only the
binary itself is faked. :class:`TestBoundedWaitFallsThrough` covers the
fall-through behaviour with a real cross-process lock holder.
"""

import threading
from pathlib import Path
from unittest.mock import patch

from teatree.core import speak as speak_mod

_FAKE_SAY = """#!/usr/bin/env python3
import sys, time
log = sys.argv[1]
with open(log, "a") as fh:
    fh.write(f"START {time.monotonic_ns()}\\n")
time.sleep(0.25)
with open(log, "a") as fh:
    fh.write(f"STOP {time.monotonic_ns()}\\n")
"""


def _install_fake_say(tmp_path: Path, log: Path) -> Path:
    """A fake ``say`` that logs START/STOP with a sleep; the log path is fixed in argv."""
    say = tmp_path / "say"
    # The real _speak_local calls ``[say_bin, text]``; the fake reads its log
    # path from argv[1] (the "text"), so each invocation writes its own marks.
    say.write_text(_FAKE_SAY, encoding="utf-8")
    say.chmod(0o755)
    return say


def _parse_events(log: Path) -> list[tuple[str, int]]:
    events: list[tuple[str, int]] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        kind, _, ns = line.partition(" ")
        events.append((kind, int(ns)))
    return events


class TestSerialLock:
    def test_concurrent_plays_do_not_overlap(self, tmp_path: Path) -> None:
        """Two concurrent ``_speak_local`` calls run ``say`` strictly serially."""
        log = tmp_path / "say.log"
        say = _install_fake_say(tmp_path, log)
        lock = tmp_path / "speak.lock"

        with (
            patch.object(speak_mod.shutil, "which", return_value=str(say)),
            patch.object(speak_mod, "_speaker_lock_path", return_value=lock),
            patch.object(speak_mod, "_is_away", return_value=False),
        ):
            threads = [threading.Thread(target=speak_mod._speak_local, args=(str(log),)) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        events = _parse_events(log)
        assert [k for k, _ in events] == ["START", "STOP", "START", "STOP"], (
            f"plays overlapped — expected strict START/STOP pairs, got {events}"
        )
        # The second START must be at-or-after the first STOP: no overlap.
        _, first_stop = events[1]
        _, second_start = events[2]
        assert second_start >= first_stop, f"second play started before the first finished: {events}"


class TestSerialLockCrossProcess:
    """The lock serializes real separate processes, not just in-process threads."""

    def test_two_subprocess_plays_do_not_overlap(self, tmp_path: Path) -> None:
        import subprocess  # noqa: PLC0415
        import sys  # noqa: PLC0415
        import textwrap  # noqa: PLC0415

        log = tmp_path / "say.log"
        say = _install_fake_say(tmp_path, log)
        lock = tmp_path / "speak.lock"
        repo_src = Path(speak_mod.__file__).resolve().parents[3]

        runner = tmp_path / "play.py"
        runner.write_text(
            textwrap.dedent(
                f"""
                import sys
                from pathlib import Path
                from unittest.mock import patch
                sys.path.insert(0, {str(repo_src)!r})
                from teatree.core import speak as speak_mod
                lock = Path({str(lock)!r})
                with (
                    patch.object(speak_mod.shutil, "which", return_value={str(say)!r}),
                    patch.object(speak_mod, "_speaker_lock_path", return_value=lock),
                    patch.object(speak_mod, "_is_away", return_value=False),
                ):
                    speak_mod._speak_local({str(log)!r})
                """
            ),
            encoding="utf-8",
        )

        procs = [subprocess.Popen([sys.executable, str(runner)]) for _ in range(2)]
        for p in procs:
            p.wait(timeout=20)

        events = _parse_events(log)
        assert [k for k, _ in events] == ["START", "STOP", "START", "STOP"], f"cross-process plays overlapped: {events}"


class TestLockPath:
    def test_lock_path_under_state_dir(self) -> None:
        """The lockfile is built from ``get_data_dir("speak")``, not an ad-hoc path."""
        from teatree.paths import get_data_dir  # noqa: PLC0415

        expected = get_data_dir(speak_mod._SPEAKER_LOCK_NAMESPACE) / speak_mod._SPEAKER_LOCK_FILENAME
        path = speak_mod._speaker_lock_path()
        assert path == expected
        assert path.name.endswith(".lock")
        assert path.parent.name == "speak"

    def test_lock_error_fails_open_and_still_plays(self, tmp_path: Path) -> None:
        """A lockfile that can't be opened must NOT mute audio — fail open, still play."""
        say = _install_fake_say(tmp_path, tmp_path / "say.log")
        with (
            patch.object(speak_mod.shutil, "which", return_value=str(say)),
            patch.object(speak_mod, "_speaker_lock_path", side_effect=OSError("no state dir")),
            patch.object(speak_mod, "_is_away", return_value=False),
            patch.object(speak_mod, "run_allowed_to_fail") as run,
        ):
            speak_mod._speak_local("hello")
        run.assert_called_once()


_HOLD_LOCK = """#!/usr/bin/env python3
import fcntl, sys, time
lock_path, ready_path, hold_s = sys.argv[1], sys.argv[2], float(sys.argv[3])
fh = open(lock_path, "a", encoding="utf-8")
fcntl.flock(fh, fcntl.LOCK_EX)
# Signal "lock held" only AFTER the exclusive flock is actually acquired.
open(ready_path, "w").close()
time.sleep(hold_s)
fcntl.flock(fh, fcntl.LOCK_UN)
fh.close()
"""


class TestBoundedWaitFallsThrough:
    """A busy speaker must NOT silence a read — the lock is best-effort, not a gate.

    A separate REAL process holds the REAL ``fcntl.flock`` on the lockfile (no
    mock of teatree code or the lock). While it is held far longer than the wait
    budget, an in-process ``_speak_local`` must return WITHIN ~the budget AND
    still invoke ``say`` — proving the bounded wait falls through to playing (no
    serialization) rather than dropping the read silently.

    Anti-vacuity: revert the fix (change ``_serial_speaker`` to return early
    without calling ``say`` when the budget elapses) and
    ``test_busy_speaker_falls_through_and_still_calls_say`` goes RED
    (``run_allowed_to_fail`` is NOT called — the read is silently dropped). It
    guards the fall-through behaviour, not just "a lock exists".
    """

    BUDGET_S = 0.3
    HOLD_S = 5.0

    def test_busy_speaker_falls_through_and_still_calls_say(self, tmp_path: Path) -> None:
        import subprocess  # noqa: PLC0415
        import sys  # noqa: PLC0415
        import time  # noqa: PLC0415

        lock = tmp_path / "speaker.lock"
        ready = tmp_path / "lock-held.ready"
        holder_script = tmp_path / "hold_lock.py"
        holder_script.write_text(_HOLD_LOCK, encoding="utf-8")

        holder = subprocess.Popen([sys.executable, str(holder_script), str(lock), str(ready), str(self.HOLD_S)])
        try:
            deadline = time.monotonic() + 10
            while not ready.exists():
                assert time.monotonic() < deadline, "holder never acquired the lock"
                assert holder.poll() is None, "holder exited before signalling"
                time.sleep(0.01)

            with (
                patch.object(speak_mod.shutil, "which", return_value="/usr/bin/say"),
                patch.object(speak_mod, "_speaker_lock_path", return_value=lock),
                patch.object(speak_mod, "_SPEAKER_LOCK_WAIT_BUDGET_S", self.BUDGET_S),
                patch.object(speak_mod, "_is_away", return_value=False),
                patch.object(speak_mod, "run_allowed_to_fail") as run,
            ):
                start = time.monotonic()
                speak_mod._speak_local("hello while the speaker is busy")
                elapsed = time.monotonic() - start

            # Falls through, not dropped: ``say`` is still called even though the lock is held.
            run.assert_called_once()
            # Latency capped at the budget — NOT the full hold a blocking acquire would wait.
            assert elapsed < self.HOLD_S, (
                f"speak blocked for {elapsed:.2f}s (>= the {self.HOLD_S}s hold) — "
                f"unbounded blocking acquire, not bounded-wait-then-fall-through"
            )
            assert elapsed < self.BUDGET_S + 1.0, f"speak took {elapsed:.2f}s — far over the {self.BUDGET_S}s budget"
        finally:
            holder.wait(timeout=self.HOLD_S + 10)

    def test_free_speaker_acquires_immediately_and_plays(self, tmp_path: Path) -> None:
        """With the lock FREE, the bounded acquire succeeds at once and ``say`` runs."""
        import time  # noqa: PLC0415

        lock = tmp_path / "speaker.lock"
        with (
            patch.object(speak_mod.shutil, "which", return_value="/usr/bin/say"),
            patch.object(speak_mod, "_speaker_lock_path", return_value=lock),
            patch.object(speak_mod, "_SPEAKER_LOCK_WAIT_BUDGET_S", self.BUDGET_S),
            patch.object(speak_mod, "_is_away", return_value=False),
            patch.object(speak_mod, "run_allowed_to_fail") as run,
        ):
            start = time.monotonic()
            speak_mod._speak_local("hello with a free speaker")
            elapsed = time.monotonic() - start
        run.assert_called_once()
        assert elapsed < self.BUDGET_S, f"free-lock acquire took {elapsed:.2f}s — should be immediate"
