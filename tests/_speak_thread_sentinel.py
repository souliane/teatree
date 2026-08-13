"""Fail the test that LEAKS a local-playback thread, not the bystander it lands in.

:func:`teatree.core.speak._maybe_speak_local` (and :func:`~teatree.core.speak.speak`)
dispatch playback on a daemon thread so the caller's egress is never delayed. That
thread reads module globals — ``shutil.which``, ``run_allowed_to_fail``,
``_speaker_lock_path`` — at CALL time, not at spawn time. A test that returns while
one is still running therefore hands it to whatever runs next: the thread resolves
the NEXT test's ``mock.patch`` objects and calls that test's mocks with its own
arguments, and the failure reads as ``Expected 'run_allowed_to_fail' to have been
called once. Called 2 times`` in a test that never spawned a thread (#4277).

Which test gets hit is decided by ``pytest-split``'s balancing, so the victim rotates
with every PR that adds or removes tests and the blame lands on an innocent diff.

Two things fix it at the source, and this sentinel is the mechanical check that one
of them was done: a test that does not need real playback patches
``speak_mod.threading.Thread``; one that does joins via :func:`join_speak_threads`.

Always on, for the same reason as :mod:`tests._thread_db_sentinel` — a guard that has
to be switched on can only ever be switched on after the fact. It costs one
:func:`threading.enumerate` per test teardown.
"""

import dataclasses
import threading

import pytest

#: Bound on joining a leaked thread before reporting it. Generous enough to cover
#: ``_speak_local``'s own worst case (the 2 s speaker-lock wait budget plus a ``say``
#: that is absent off macOS), because the join is what CONTAINS the leak — a thread
#: still running past this is reported as unjoined rather than waited on forever.
JOIN_TIMEOUT_S = 5.0


class LeakedSpeakThreadError(AssertionError):
    """A test returned while a local-playback thread was still running."""


@dataclasses.dataclass(frozen=True, slots=True)
class SpeakThreadLeak:
    """One playback thread that outlived the test that spawned it."""

    ident: int | None
    joined: bool


def _speak_thread_name() -> str:
    """The canonical playback-thread name, read from the module that spawns them.

    Imported at call time: ``teatree.core.speak`` pulls the config layer in, and this
    module is imported from ``conftest`` at collection.
    """
    from teatree.core.speak import SPEAK_THREAD_NAME  # noqa: PLC0415 — deferred, see docstring

    return SPEAK_THREAD_NAME


def live_speak_threads() -> list[threading.Thread]:
    """Every local-playback thread currently running in this process."""
    name = _speak_thread_name()
    return [t for t in threading.enumerate() if t.name == name and t.is_alive()]


def join_speak_threads(timeout: float = JOIN_TIMEOUT_S) -> list[SpeakThreadLeak]:
    """Join every live playback thread, returning one record per thread found.

    The sentinel calls this to CONTAIN a leak before failing its opener; a test that
    legitimately wants real playback calls it to hand the thread back before it
    returns. Same primitive either way — a caller that joins leaves nothing to find.
    """
    threads = live_speak_threads()
    for thread in threads:
        thread.join(timeout=timeout)
    return [SpeakThreadLeak(ident=t.ident, joined=not t.is_alive()) for t in threads]


def describe(leaks: "list[SpeakThreadLeak]", *, detected_in: str) -> str:
    """The failure message naming the leaking test and both ways to fix it."""
    unjoined = [leak for leak in leaks if not leak.joined]
    lines = [
        f"{len(leaks)} local-playback thread(s) were still running when {detected_in} returned.",
        "",
        "A playback thread resolves teatree.core.speak's module globals when it RUNS, so a",
        "leaked one lands inside the next test, picks up that test's mock.patch objects and",
        "calls that test's mocks — reddening a test that never spawned a thread (#4277).",
        "",
        "Fix it in the test that spawned it:",
        "  - no real playback needed -> patch.object(speak_mod.threading, 'Thread'), as the",
        "    sibling tests in tests/teatree_core/test_speak.py do;",
        "  - real playback needed -> tests._speak_thread_sentinel.join_speak_threads() before",
        "    the test returns.",
    ]
    if unjoined:
        lines += [
            "",
            f"{len(unjoined)} of them did not finish within {JOIN_TIMEOUT_S}s and are STILL running,",
            "so later tests in this worker may still be polluted.",
        ]
    return "\n".join(lines)


class SpeakThreadSentinel:
    """Pytest plugin: red the test that leaves a local-playback thread running."""

    def __init__(self, *, join_timeout: float = JOIN_TIMEOUT_S) -> None:
        self._join_timeout = join_timeout

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_teardown(self, item: pytest.Item) -> None:
        # ``trylast`` runs this AFTER pytest's own teardown impl, so a fixture finalizer
        # is still free to be the thing that joins. A plain hookimpl rather than a
        # hookwrapper: pluggy downgrades a raise from an old-style wrapper to a
        # ``PluggyTeardownRaisedWarning``, which only reds while ``filterwarnings =
        # error`` holds, and a new-style wrapper cannot both ``yield`` and return.
        leaks = join_speak_threads(self._join_timeout)
        if leaks:
            raise LeakedSpeakThreadError(describe(leaks, detected_in=item.nodeid))
