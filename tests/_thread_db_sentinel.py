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

The module also holds the guard for the OTHER half of this failure class, which
produced the strands actually observed in CI: Django decorates
``BaseDatabaseWrapper.connect`` with ``@async_unsafe``, so a thread that has a
running event loop is REFUSED a connection with ``SynchronousOnlyOperation``.
``DJANGO_ALLOW_ASYNC_UNSAFE`` disables that refusal process-wide, and the connect
then silently succeeds against a fresh EMPTY ``:memory:`` database — the exact
"no such table" + stranded-handle pair. See
:func:`assert_async_unsafe_guard_intact`.
"""

import dataclasses
import os
import threading
import traceback
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

#: Django's escape hatch for its own async-unsafe guard. The unit lane must never
#: run with it set — see :func:`assert_async_unsafe_guard_intact`.
ASYNC_UNSAFE_ENV = "DJANGO_ALLOW_ASYNC_UNSAFE"

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


class AsyncUnsafeGuardDisabledError(RuntimeError):
    """The unit lane is running with Django's async-unsafe guard switched off."""

    def __init__(self) -> None:
        super().__init__(
            f"{ASYNC_UNSAFE_ENV} is set for the unit test suite. Django's async-unsafe guard "
            "on BaseDatabaseWrapper.connect is what refuses a connection to a thread that owns "
            "a running event loop; with it off, that connect silently opens a fresh EMPTY "
            ":memory: database, every config read logs `no such table`, and the handle is "
            "stranded for a later GC to red an unrelated test.\n\n"
            "It is almost always set by a collection-time import side effect — a module under "
            "tests/ importing the e2e lane's conftest, whose body sets it. Import the helper "
            "from a plain module instead of the conftest, and leave the variable to the e2e lane."
        )


def assert_async_unsafe_guard_intact(environ: "dict[str, str] | None" = None) -> None:
    """Refuse to run the unit suite with ``DJANGO_ALLOW_ASYNC_UNSAFE`` set.

    Django's ``@async_unsafe`` decorator on ``BaseDatabaseWrapper.connect`` is what
    stops a thread that owns a running event loop from opening its own connection.
    With the guard off, that connect succeeds instead of raising — and under the
    ``:memory:`` test database it lands on a brand-new EMPTY database, so every
    config/ORM read logs ``no such table`` and the handle is stranded for a later
    GC to finalize as ``ResourceWarning: unclosed database``.

    The guard is disabled process-wide by a single ``os.environ`` write, and the
    write that actually happened was a COLLECTION-TIME side effect: a module under
    ``tests/`` imported the e2e lane's ``conftest``, whose module body sets the
    variable. Collection imports every test module in every shard, so one stray
    import poisons all twelve — even the eleven that then deselect the importing
    file. That is invisible to the per-test env diff in
    ``scripts/ci/leak_sentinel_plugin.py`` (the mutation predates every test's
    baseline), and equally invisible to a session-START check (it predates the
    import), so this is asserted when collection FINISHES.

    The e2e lane legitimately sets it and does not load ``tests/conftest.py``, so
    this is scoped to the unit lane only.
    """
    env = os.environ if environ is None else environ
    if env.get(ASYNC_UNSAFE_ENV):
        raise AsyncUnsafeGuardDisabledError


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

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Refuse the run when collection left the async-unsafe guard disabled.

        Asserted HERE, not at session start: the write that disables it is a
        module-body side effect of importing a test module, so it only exists once
        every test module has been imported. Failing loud on the cause beats
        reporting the cascade of stranded handles it produces.

        Re-raised as a :class:`pytest.UsageError` (which is ``@final``, so it
        cannot simply be the exception's base class) so the session aborts with a
        plain readable message rather than an INTERNALERROR traceback — the reader
        needs the cause and the fix, not this hook's own stack.
        """
        del session  # hookspec signature; the check is process-global
        try:
            assert_async_unsafe_guard_intact()
        except AsyncUnsafeGuardDisabledError as exc:
            raise pytest.UsageError(str(exc)) from exc

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
