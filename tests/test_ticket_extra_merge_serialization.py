"""Concurrency regression for souliane/teatree#800 N3 — REAL primitive.

Drives the **real** ``Ticket.merge_extra`` (not a hand-written SQL
copy) from two real threads against a shared **file-backed SQLite**
configured exactly like the #804 production ``default`` database
(``SQLITE_WRITE_SERIALIZATION_OPTIONS``). The pytest-django test DB is
``:memory:`` (per-connection, single-threaded — no contention is even
possible there), so this module stands up its own migrated file-backed
DB and rebinds ``connections.databases['default']`` to it for the test,
so ``Ticket.objects`` / ``ticket.merge_extra`` genuinely contend.

Anti-vacuity is on the SHIPPED method: with the real
``Ticket.merge_extra`` body reverted to the pre-fix unlocked shape
(``self.extra = …; self.save(update_fields=["extra"])`` with no
``select_for_update`` re-read) the concurrent-writers test goes RED
(one writer's key is clobbered); with the shipped locked re-read it is
GREEN (both keys survive). The ``_pre_fix_merge_extra`` monkeypatch
parametrization runs the SAME harness against the pre-fix body and MUST
fail — that is the genuine mutation-revert proof.

Backend under test: file-backed ``django.db.backends.sqlite3`` with the
real production OPTIONS, two OS threads, two connections.
"""

import contextlib
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connections

from teatree.core.models import Ticket
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS


@pytest.fixture
def file_backed_default(tmp_path: Path, django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    """Rebind the ``default`` DB to a migrated file-backed SQLite.

    The real ORM (``Ticket.objects``) targets ``default``; pointing
    ``default`` at a real file (not ``:memory:``) is what lets two
    threads contend on the same row through the real method.
    """
    db_file = tmp_path / f"n3_{uuid.uuid4().hex}.sqlite3"
    original = connections.databases["default"]

    def _drop_cached_default() -> None:
        # ``connections`` caches per-thread; only the thread that opened
        # the alias has it. Close+evict defensively (the main thread may
        # never have opened ``:memory:``).
        with contextlib.suppress(AttributeError):
            connections["default"].close()
        with contextlib.suppress(AttributeError):
            del connections["default"]

    _drop_cached_default()
    connections.databases["default"] = {
        **original,
        "NAME": str(db_file),
        "OPTIONS": dict(SQLITE_WRITE_SERIALIZATION_OPTIONS),
        "CONN_MAX_AGE": 0,
        "TEST": {},
    }
    with django_db_blocker.unblock():
        call_command("migrate", run_syncdb=True, verbosity=0)
        connections["default"].close()  # close the migrate connection now
        yield
    for conn in connections.all():
        conn.close()
    _drop_cached_default()
    connections.databases["default"] = original


def _merge_in_thread(ticket_pk: int, key: str, value: object, barrier: threading.Barrier) -> None:
    """Load the row fresh, then call the REAL Ticket.merge_extra."""
    try:
        ticket = Ticket.objects.get(pk=ticket_pk)
        barrier.wait(timeout=10)
        ticket.merge_extra(set_keys={key: value})
    finally:
        for conn in connections.all():
            conn.close()


def _run_two_merges(ticket_pk: int) -> dict:
    barrier = threading.Barrier(2)
    work: list[tuple[str, object]] = [("pr_urls", ["u"]), ("visual_qa", {"errors": 0})]

    def runner(item: tuple[str, object]) -> None:
        _merge_in_thread(ticket_pk, item[0], item[1], barrier)

    threads = [threading.Thread(target=runner, args=(w,)) for w in work]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return Ticket.objects.get(pk=ticket_pk).extra


def _pre_fix_merge_extra(self: Ticket, *, set_keys: dict | None = None, pop_keys: list | None = None) -> None:
    """The PRE-FIX unlocked shape: stale in-memory extra, no locked re-read.

    Exactly what the N3 sites did before #800 — mutate the (possibly
    stale) in-memory ``self.extra`` and ``save(update_fields=["extra"])``
    with no ``transaction.atomic()`` + ``select_for_update`` re-read.
    """
    merged = dict(self.extra or {})
    if set_keys:
        merged.update(set_keys)
    for key in pop_keys or []:
        merged.pop(key, None)
    self.extra = merged
    self.save(update_fields=["extra"])


@pytest.mark.usefixtures("file_backed_default")
class TestTicketMergeExtraRealPrimitiveSerialization:
    """Two real threads call the REAL ``Ticket.merge_extra`` on one row."""

    def test_real_merge_extra_preserves_both_concurrent_keys_green(self) -> None:
        pk = Ticket.objects.create(extra={}).pk
        final = _run_two_merges(pk)
        # The shipped locked re-read: both writers' keys survive.
        assert final.get("pr_urls") == ["u"], final
        assert final.get("visual_qa") == {"errors": 0}, final

    def test_pre_fix_unlocked_merge_extra_clobbers_a_key_red(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The SAME harness against the PRE-FIX body must fail.

        Monkeypatching ``merge_extra`` to the unlocked pre-fix shape
        reproduces the lost-update on the prod backend, proving the
        green test guards the real shipped fix and is not vacuous.
        """
        monkeypatch.setattr(Ticket, "merge_extra", _pre_fix_merge_extra)
        pk = Ticket.objects.create(extra={}).pk
        final = _run_two_merges(pk)
        # Pre-fix: one writer's whole-dict save clobbers the other key.
        assert not ("pr_urls" in final and "visual_qa" in final), (
            f"pre-fix unlocked merge_extra unexpectedly kept both keys: {final}"
        )
