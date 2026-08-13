"""Concurrent ``enqueue_phase_task_once`` queues exactly ONE task on prod SQLite (#4271).

The dashboard's enqueue buttons are POST targets, so a double-click — or any repeated
POST — races two callers into
:func:`~teatree.core.models.task_enqueue.enqueue_phase_task_once` for one ticket and
phase. Queueing twice dispatches two headless agents at the same phase of the same
ticket, which is why the guard's probe and create share one ``transaction.atomic()``.

Nothing pinned that. ``select_for_update()`` is mechanically inert here
(``has_select_for_update`` is False on Django's SQLite backend, so no ``FOR UPDATE``
clause is emitted); the serialization comes from ``transaction.atomic()`` under
``transaction_mode="IMMEDIATE"``, where ``BEGIN IMMEDIATE`` takes SQLite's reserved
write lock at transaction start. ``tests/django_settings.py`` runs on ``:memory:`` with
no ``transaction_mode``, so the mechanism that provides the guarantee is absent while
the tests run — removing both statements left the whole suite green.

This module therefore migrates its own file-backed SQLite under ``tmp_path``, points
``default`` at it under the production ``SQLITE_WRITE_SERIALIZATION_OPTIONS`` for the
duration of the race (the guard takes no ``using=``, so the connection it serializes on
has to BE ``default``), and drives K real threads through the real seam.

Anti-vacuity: :func:`_enqueue_without_the_guard` is the guard's body with
``transaction.atomic()`` and ``select_for_update()`` removed — the exact mutation the
issue measured — assembled from the same ``_unstarted_tasks`` probe and the same
``enqueue_phase_task`` create, so it is the shipped code minus the guard rather than an
approximation of it. Raced through the same harness every caller creates a row, which
is what makes the green a measurement rather than an absence of evidence.
"""

import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import DEFAULT_DB_ALIAS, connections
from django.test import override_settings

from teatree.core.models.task import Task
from teatree.core.models.task_enqueue import (
    DuplicatePhaseTaskError,
    _unstarted_tasks,
    enqueue_phase_task,
    enqueue_phase_task_once,
)
from teatree.core.models.ticket import Ticket
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS
from tests.db_alias import RouteAllToAlias, register_sqlite_alias, run_racing_threads, teardown_sqlite_alias

_PHASE = "reviewing"
_REASON = "Review now."
_CALLERS = 8

_CREATED = "created"
_REFUSED = "refused"

Enqueue = Callable[[Ticket], Task]


def _enqueue_with_the_shipped_guard(ticket: Ticket) -> Task:
    return enqueue_phase_task_once(ticket=ticket, phase=_PHASE, reason=_REASON)


def _enqueue_without_the_guard(ticket: Ticket) -> Task:
    """``enqueue_phase_task_once`` with ``atomic()`` and ``select_for_update()`` removed.

    Built from the seam's own probe and create so it stays the shipped body minus the
    guard, with no window-widening hold to manufacture the race. ``select_for_update()``
    goes with the ``atomic()`` because Django refuses it outside a transaction.
    """
    existing = _unstarted_tasks(ticket, _PHASE).first()
    if existing is not None:
        msg = f"TODO-{existing.pk} is already queued for {_PHASE} — nothing to enqueue."
        raise DuplicatePhaseTaskError(msg)
    return enqueue_phase_task(ticket=ticket, phase=_PHASE, reason=_REASON, agent_id="dashboard")


def _migrated_file_backed_db(tmp_path: Path) -> tuple[str, Path]:
    """A private alias migrated to HEAD on its own file.

    ``RouteAllToAlias`` is installed for the migrate because the ``core``
    ``0001_initial`` seed reads historical models with no ``using=``, which Django would
    otherwise resolve onto the shared ``default`` test database.
    """
    alias = f"enq_{uuid.uuid4().hex}"
    db_file = tmp_path / f"{alias}.sqlite3"
    register_sqlite_alias(alias, db_file)
    with override_settings(DATABASE_ROUTERS=[RouteAllToAlias(alias)]):
        call_command("migrate", "--no-input", database=alias, verbosity=0)
    return alias, db_file


@contextmanager
def _default_pointed_at(db_file: Path) -> Iterator[None]:
    """Point ``default`` at *db_file* under the production write-serialization OPTIONS.

    The settings dict is mutated in place rather than replaced: the already-open
    main-thread connection holds a reference to this very dict, and closing it to pick
    up a replacement would destroy the shared-cache ``:memory:`` test database the rest
    of the worker's tests run against. The racing threads hold no ``default`` connection
    yet, so each builds its own from the mutated settings — which is the
    cross-connection contention the race needs.
    """
    settings_dict = connections.databases[DEFAULT_DB_ALIAS]
    original = {key: settings_dict[key] for key in ("NAME", "OPTIONS")}
    settings_dict["NAME"] = str(db_file)
    settings_dict["OPTIONS"] = dict(SQLITE_WRITE_SERIALIZATION_OPTIONS)
    try:
        yield
    finally:
        settings_dict.update(original)


def _race_enqueues(ticket_pk: int, enqueue: Enqueue) -> list[str]:
    """K real threads call *enqueue* on the same ticket, released together by a barrier.

    Each thread reads its own ``Ticket`` first so its ``default`` connection is already
    open when the barrier drops — otherwise connection setup, not the guard, decides who
    gets there first.
    """
    barrier = threading.Barrier(_CALLERS)

    def caller(_idx: int) -> str:
        ticket = Ticket.objects.get(pk=ticket_pk)
        barrier.wait(timeout=30)
        try:
            enqueue(ticket)
        except DuplicatePhaseTaskError:
            return _REFUSED
        return _CREATED

    return run_racing_threads(caller, _CALLERS, timeout=60.0)


@pytest.fixture
def _unblocked_db(django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    """Lift pytest-django's DB-access guard — this module owns its file-backed database.

    No ``django_db`` marker is used: a rollback wrapper would undo the real
    cross-connection commits the race is measuring.
    """
    with django_db_blocker.unblock():
        yield


@pytest.mark.usefixtures("_unblocked_db")
class TestEnqueuePhaseTaskOnceUnderConcurrentCallers:
    """K real threads race the dashboard's enqueue seam on a file-backed SQLite."""

    def _race(self, tmp_path: Path, enqueue: Enqueue) -> tuple[list[str], int]:
        alias, db_file = _migrated_file_backed_db(tmp_path)
        try:
            ticket = Ticket.objects.using(alias).create(overlay="test")
            connections[alias].close()
            with _default_pointed_at(db_file):
                outcomes = _race_enqueues(ticket.pk, enqueue)
            rows = Task.objects.using(alias).filter(ticket=ticket, phase=_PHASE).count()
        finally:
            teardown_sqlite_alias(alias)
        return outcomes, rows

    def test_concurrent_callers_queue_one_task_and_the_losers_are_refused(self, tmp_path: Path) -> None:
        """The shipped guard: one caller creates, every other is refused, one row lands."""
        outcomes, rows = self._race(tmp_path, _enqueue_with_the_shipped_guard)

        assert outcomes.count(_CREATED) == 1, f"expected exactly one create, got {outcomes}"
        assert outcomes.count(_REFUSED) == _CALLERS - 1, f"expected {_CALLERS - 1} refusals, got {outcomes}"
        assert rows == 1, f"expected exactly one queued task in the database, found {rows}"

    def test_the_same_race_double_enqueues_once_the_guard_is_removed(self, tmp_path: Path) -> None:
        """Anti-vacuity: without ``atomic()`` the callers all probe empty and all create.

        Measured at 8 of 8 on this harness. The assertion is the contract the guard
        exists to hold — *exactly one* — rather than that count, so a caller that happens
        to probe after a rival's commit cannot make the control flaky.
        """
        outcomes, rows = self._race(tmp_path, _enqueue_without_the_guard)

        assert outcomes.count(_CREATED) > 1, f"the harness cannot detect a double-enqueue: {outcomes}"
        assert rows == outcomes.count(_CREATED), f"{rows} rows for {outcomes.count(_CREATED)} creates"
