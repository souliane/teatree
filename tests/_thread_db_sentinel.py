"""Fail the thread that STRANDS a DB handle, not the bystander test that GCs it.

A worker thread that touches the ORM gets its own thread-local Django connection
(``django.db.connections`` is an ``asgiref.local.Local``). If it exits without
closing the raw DB-API handle, CPython finalizes that handle at an arbitrary
later garbage collection and emits ``ResourceWarning: unclosed database``. Under
this repo's ``filterwarnings = ["error", ...]`` the warning becomes a hard
``PytestUnraisableExceptionWarning`` attributed to whatever unrelated test
happened to be running in that xdist worker — a rotating cast of innocent
victims, one per shard, with nothing in the traceback pointing at the culprit.

Under the ``:memory:`` test database the same leak also produces a loud
``no such table: <table>`` from any config/ORM read on that thread: a fresh
in-memory connection is a fresh EMPTY database, not the migrated one the main
thread restored from the template.

:func:`teatree.utils.thread_db.close_thread_db_connections` is the fix a
thread-spawning site must call in a ``finally``. This sentinel is the mechanical
check that every such site actually does: it wraps ``BaseDatabaseWrapper.connect``,
records each non-main-thread open with the test that caused it, and at every test
teardown fails the run for any handle whose owning thread has DIED while the
handle is still open — naming the opening test and the stack that opened it.

Always on, because the failure it catches lands on a random test in a random
shard: a sentinel that has to be switched on can only ever be switched on after
the fact. It costs one ``threading.current_thread()`` comparison per connection
open; the stack capture only runs for the rare non-main-thread open.
"""

import dataclasses
import threading
import traceback
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

#: Frames of pytest/threading/sentinel plumbing that carry no information about
#: the site that opened the connection.
_UNINTERESTING_FRAME_MARKERS = (
    "/_pytest/",
    "/site-packages/pluggy/",
    "lib/python3",
    "/threading.py",
    "_thread_db_sentinel.py",
)

#: How many surviving frames of the opening stack to show in the failure.
_STACK_FRAMES_SHOWN = 6


class StrandedDbHandleError(AssertionError):
    """A worker thread died holding an open raw DB handle."""


def handle_is_open(raw: Any) -> bool:
    """Whether *raw* (a DB-API connection) still holds an open handle.

    Deliberately does NOT execute SQL: the owning thread is gone, and a probe
    query would be I/O on a connection nobody owns. ``sqlite3`` raises
    ``ProgrammingError`` on any attribute touch after ``close()``; psycopg
    exposes a ``closed`` flag. An unrecognised backend is assumed open, so a
    future backend fails loud rather than silently green.
    """
    closed = getattr(raw, "closed", None)
    if closed is not None:
        return not closed
    try:
        raw.in_transaction  # noqa: B018 — sqlite3 raises ProgrammingError once closed
    except AttributeError:
        return True
    except Exception:  # noqa: BLE001 — any refusal to answer means the handle is gone
        return False
    return True


@dataclasses.dataclass(slots=True)
class OpenedConnection:
    """One non-main-thread connection open, kept until its thread is proven clean."""

    wrapper: Any
    thread: threading.Thread
    nodeid: str
    stack: str


def _opening_stack() -> str:
    """The call stack at the open, minus pytest/threading/sentinel plumbing."""
    raw_frames = traceback.format_stack()[:-2]
    frames = [line for line in raw_frames if not any(m in line for m in _UNINTERESTING_FRAME_MARKERS)]
    return "".join((frames or raw_frames)[-_STACK_FRAMES_SHOWN:])


class ThreadDbHandleSentinel:
    """Pytest plugin: red the run when a worker thread strands a raw DB handle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._opened: list[OpenedConnection] = []
        self._nodeid = "<session>"
        self._installed = False

    def install(self) -> None:
        """Wrap ``BaseDatabaseWrapper.connect`` so every open is recorded."""
        from django.db.backends.base.base import BaseDatabaseWrapper  # noqa: PLC0415 — Django import at call time

        if self._installed:
            return
        original = BaseDatabaseWrapper.connect
        sentinel = self

        def connect(wrapper: Any) -> Any:
            result = original(wrapper)
            sentinel.record_open(wrapper)
            return result

        BaseDatabaseWrapper.connect = connect  # ty: ignore[invalid-assignment]
        self._installed = True

    def record_open(self, wrapper: Any) -> None:
        """Register *wrapper* when the opening thread is not the main thread."""
        thread = threading.current_thread()
        if thread is threading.main_thread():
            return
        with self._lock:
            self._opened.append(OpenedConnection(wrapper, thread, self._nodeid, _opening_stack()))

    def sweep(self) -> list[OpenedConnection]:
        """Release and return every recorded open whose thread died holding the handle.

        A record whose thread is still alive stays under watch — a live worker's
        connection is in use, not stranded. A record whose owner closed properly
        is simply dropped.
        """
        stranded: list[OpenedConnection] = []
        with self._lock:
            pending, self._opened = self._opened, []
            for record in pending:
                if record.thread.is_alive():
                    self._opened.append(record)
                    continue
                raw = record.wrapper.connection
                if raw is None or not handle_is_open(raw):
                    continue
                stranded.append(record)
                # Release it here so ONE leak reds its own opener rather than
                # cascading into an unraisable warning on a later bystander.
                try:
                    raw.close()
                finally:
                    record.wrapper.connection = None
        return stranded

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        """Install the wrapper once Django is configured (after pytest-django's setup)."""
        del session  # hookspec signature; the sentinel is session-independent
        self.install()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item) -> "Generator[None, object]":
        self._nodeid = item.nodeid
        yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_teardown(self, item: pytest.Item) -> "Generator[None, object]":
        yield  # let the real teardown join any threads the test started
        stranded = self.sweep()
        if not stranded:
            return
        raise StrandedDbHandleError(describe(stranded, detected_in=item.nodeid))


def describe(stranded: "list[OpenedConnection]", *, detected_in: str) -> str:
    """The failure message naming each stranding site and how to fix it."""
    lines = [
        (
            f"{len(stranded)} worker thread(s) exited holding an open Django DB handle "
            f"(detected at the teardown of {detected_in})."
        ),
        "",
        (
            "A stranded handle is finalized by a later garbage collection, which under "
            "this suite's `filterwarnings = error` reds an unrelated bystander test in "
            "whatever xdist worker happens to be running — the flake this sentinel exists "
            "to prevent."
        ),
        "",
        (
            "Fix: call `teatree.utils.thread_db.close_thread_db_connections()` in a "
            "`finally` on the thread target below."
        ),
    ]
    for record in stranded:
        lines += [
            "",
            f"  thread {record.thread.name!r} opened during {record.nodeid}:",
            record.stack.rstrip(),
        ]
    return "\n".join(lines)
