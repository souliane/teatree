# test-path: cross-cutting — tests tests/_speak_thread_sentinel.py (test infra); the src imports are
# the anti-vacuity subject, not the unit under test.
"""Tests for ``tests/_speak_thread_sentinel.py`` — the leaked-playback-thread sentinel.

A local-playback thread that outlives its test resolves ``teatree.core.speak``'s module
globals inside the NEXT test, so it calls that test's mocks and reds a diff that never
touched speak (#4277). The sentinel turns that rotating bystander failure into a named
one on the test that leaked.

Both directions are pinned at the real seam, so the guard cannot go vacuous:

* both PRODUCTION spawn sites really name their thread, so no rename can blind the guard;
* a live playback thread at teardown fails its own test, naming it;
* the sentinel JOINS what it found, so the leak stops at its opener;
* a thread it cannot join within the budget is reported as still running;
* a test that leaks nothing is not failed.
"""

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.core import speak as speak_mod
from teatree.core.speak import SPEAK_THREAD_NAME
from teatree.types import LocalPlayback, SpeakConfig
from tests._speak_thread_sentinel import (
    LeakedSpeakThreadError,
    SpeakThreadLeak,
    SpeakThreadSentinel,
    describe,
    join_speak_threads,
    live_speak_threads,
)

#: Long enough that enumeration (microseconds after the spawn) always sees the thread
#: alive, short enough that a joining sentinel returns promptly.
_ALIVE_WINDOW_S = 0.25


def _leak_a_blocked_thread(release: threading.Event) -> threading.Thread:
    """Spawn a playback-named thread that runs until *release* is set."""
    thread = threading.Thread(target=release.wait, kwargs={"timeout": 30}, daemon=True, name=SPEAK_THREAD_NAME)
    thread.start()
    return thread


def _drive_teardown(sentinel: SpeakThreadSentinel, nodeid: str) -> None:
    """Call the teardown hook the way pytest does, surfacing whatever it raises."""
    sentinel.pytest_runtest_teardown(SimpleNamespace(nodeid=nodeid))


class TestProductionThreadsAreNamed:
    """The guard matches on a name, so production must really be the thing that sets it."""

    def test_speak_names_its_playback_thread(self) -> None:
        release = threading.Event()
        with (
            patch.object(speak_mod, "resolve_speak", return_value=SpeakConfig(local=LocalPlayback.ALL)),
            patch.object(speak_mod, "_speak_local", lambda _text: release.wait(timeout=30)),
        ):
            speak_mod.speak("hello")
            try:
                assert [t.name for t in live_speak_threads()] == [SPEAK_THREAD_NAME]
            finally:
                release.set()
                join_speak_threads()

    def test_maybe_speak_local_names_its_playback_thread(self) -> None:
        release = threading.Event()
        with patch.object(speak_mod, "_speak_local", lambda _text: release.wait(timeout=30)):
            speak_mod._maybe_speak_local(SpeakConfig(local=LocalPlayback.DM), "hello")
            try:
                assert [t.name for t in live_speak_threads()] == [SPEAK_THREAD_NAME]
            finally:
                release.set()
                join_speak_threads()


class TestWhyALeakIsHarmful:
    """The harm claim itself, deterministically: globals resolve when the thread RUNS."""

    def test_a_running_thread_calls_mocks_installed_after_it_spawned(self, tmp_path: Path) -> None:
        release = threading.Event()

        def _parked_in_meeting() -> bool:
            release.wait(timeout=30)
            return False

        with patch.object(speak_mod, "_in_meeting", _parked_in_meeting):
            speak_mod._maybe_speak_local(SpeakConfig(local=LocalPlayback.DM), "the polluter's text")
            # Everything inside models the NEXT test: its patches go up AFTER the spawn,
            # and are still what the parked thread resolves once it is let go.
            with (
                patch.object(speak_mod.shutil, "which", return_value="/usr/bin/say"),
                patch.object(speak_mod, "_speaker_lock_path", return_value=tmp_path / "speaker.lock"),
                patch.object(speak_mod, "run_allowed_to_fail") as the_next_tests_mock,
            ):
                release.set()
                assert [leak.joined for leak in join_speak_threads()] == [True]
                the_next_tests_mock.assert_called_once()
                assert the_next_tests_mock.call_args.args[0] == ["/usr/bin/say", "the polluter's text"]


class TestSentinelFailsTheLeaker:
    def test_a_live_playback_thread_fails_its_own_test(self) -> None:
        release = threading.Event()
        thread = _leak_a_blocked_thread(release)
        try:
            with pytest.raises(LeakedSpeakThreadError) as excinfo:
                _drive_teardown(SpeakThreadSentinel(join_timeout=0.05), "tests/x.py::test_leaks")
        finally:
            release.set()
            thread.join(timeout=5)
        message = str(excinfo.value)
        assert "tests/x.py::test_leaks" in message
        assert "STILL running" in message, "an unjoinable thread must be reported as still running"

    def test_the_sentinel_joins_what_it_found(self) -> None:
        threading.Thread(target=time.sleep, args=(_ALIVE_WINDOW_S,), daemon=True, name=SPEAK_THREAD_NAME).start()
        with pytest.raises(LeakedSpeakThreadError) as excinfo:
            _drive_teardown(SpeakThreadSentinel(), "tests/x.py::test_leaks")
        assert not live_speak_threads(), "the leak must stop at its opener, not run on"
        assert "STILL running" not in str(excinfo.value)

    def test_a_clean_test_is_not_failed(self) -> None:
        assert not live_speak_threads()
        _drive_teardown(SpeakThreadSentinel(), "tests/x.py::test_is_clean")

    def test_joining_nothing_reports_nothing(self) -> None:
        assert join_speak_threads() == []


class TestFailureMessage:
    def test_names_both_fixes(self) -> None:
        message = describe([SpeakThreadLeak(ident=1, joined=True)], detected_in="tests/x.py::test_leaks")
        assert "speak_mod.threading" in message
        assert "join_speak_threads" in message
        assert "STILL running" not in message


class TestSentinelIsArmed:
    def test_registered_for_this_session(self, pytestconfig: pytest.Config) -> None:
        assert pytestconfig.pluginmanager.get_plugin("speak-thread-sentinel") is not None
