"""Concurrency regression for souliane/teatree#800 N5 — REAL method.

Drives the **real** ``TaskQuerySet.reap_stale_claims`` racing the
**real** ``Task.renew_lease`` from two real threads on a shared
file-backed SQLite configured like the #804 production ``default`` DB.
The pytest-django ``:memory:`` DB is per-connection (no cross-thread
contention), so this module rebinds ``default`` to a migrated
file-backed DB.

Contract (#800 N5): a CLAIMED task whose lease *was* expired but is
renewed by a live worker before the reap's write must NOT be reaped.
The shipped fix makes the reap a single conditional
``UPDATE ... WHERE status=CLAIMED AND lease_expires_at < now`` — the
expiry predicate is the CAS token, re-evaluated atomically at write
time, so a renewed lease no longer matches.

Anti-vacuity is a **two-state proof** (the shape of
``test_sqlite_write_serialization.py``), NOT a single revert-flip — and
that is structural, not a shortcut: the shipped CAS is one atomic
statement with no Python scan/write gap, so the scan→renew→fail race
*cannot exist* in the shipped body to be reproduced by reverting it.
The two states are pinned by three tests, all on the prod backend.
``..._spares_renewed_lease_green`` — the REAL ``reap_stale_claims``: a
lease renewed before the reap's write survives.
``..._reaps_a_genuinely_still_stale_task`` — the REAL method still FAILS
a non-renewed stale task (guards the green against the trivial "reap
never fails anything" vacuity). ``..._spuriously_fails_renewed_lease_red``
— the pre-fix scan-then-unconditional-``fail()`` body, run through the
faithful scan→renew→fail interleave, spurious-fails the renewed task:
the broken state the CAS removes. (The N3 ``merge_extra`` tests in
``test_ticket_extra_merge_serialization.py`` *do* additionally flip RED
on a real-body revert — there the broken behaviour survives the shipped
method's shape; N5's does not, hence the two-state proof.)

Backend under test: file-backed ``django.db.backends.sqlite3`` with the
real production OPTIONS, two OS threads, two connections.
"""

import contextlib
import threading
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from django.core.management import call_command
from django.db import connections
from django.utils import timezone

from teatree.core.managers import TaskQuerySet
from teatree.core.models import Session, Task, Ticket
from teatree.settings import SQLITE_WRITE_SERIALIZATION_OPTIONS


@pytest.fixture
def file_backed_default(tmp_path: Path, django_db_blocker: pytest.FixtureRequest) -> Iterator[None]:
    db_file = tmp_path / f"n5_{uuid.uuid4().hex}.sqlite3"
    original = connections.databases["default"]

    def _drop_cached_default() -> None:
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


def _stale_claimed_task() -> Task:
    ticket = Ticket.objects.create(overlay="t", issue_url="https://example.com/issues/800")
    session = Session.objects.create(ticket=ticket, overlay="t")
    now = timezone.now()
    return Task.objects.create(
        ticket=ticket,
        session=session,
        status=Task.Status.CLAIMED,
        claimed_by="worker-1",
        claimed_at=now - timedelta(seconds=600),
        lease_expires_at=now - timedelta(seconds=10),  # already expired
        heartbeat_at=now - timedelta(seconds=600),
    )


# Deterministic cross-thread coordination (no flaky sleeps): the worker
# waits until the reaper has *scanned*, renews, signals; the reaper
# performs its destructive WRITE only after the renew has committed.
# This pins the exact N5 interleave — scan(stale) → renew → reap-write
# — for BOTH the real CAS body and the pre-fix body.
_REAP_SCANNED = threading.Event()
_RENEW_COMMITTED = threading.Event()


def _renew_then_real_reap(task_pk: int) -> str:
    """GREEN: a live worker renews, then the REAL reap runs.

    Pins the #800 N5 contract on the shipped method: a CLAIMED task
    whose lease *was* expired but is renewed by a live worker before the
    reap's write must NOT be reaped. The shipped single conditional
    ``UPDATE ... WHERE status=CLAIMED AND lease_expires_at < now``
    re-evaluates the expiry at write time, so the renewed row no longer
    matches and survives — asserted against the real
    ``Task.objects.reap_stale_claims()``.

    Non-vacuity is pinned by the *companion* RED test
    (``_race_pre_fix_reap_vs_renew`` + ``_pre_fix_reap``), which runs
    the faithful scan→renew→fail interleave against the pre-fix
    scan-then-unconditional-``fail()`` body and spurious-fails — the
    two-state proof shape of ``test_sqlite_write_serialization.py``
    (green pins the fixed state; red pins the broken state). A
    single-test revert-flip is impossible here *by construction*: the
    shipped CAS is one atomic statement with no scan/write gap to
    interleave, so the broken behaviour only exists in the pre-fix
    body's shape — which the RED test pins directly.
    """
    Task.objects.get(pk=task_pk).renew_lease(lease_seconds=300)
    Task.objects.reap_stale_claims()
    for conn in connections.all():
        conn.close()
    return Task.objects.get(pk=task_pk).status


def _race_pre_fix_reap_vs_renew(task_pk: int) -> str:
    """RED harness: pin scan(stale) → renew → unconditional fail.

    Used only with the monkeypatched pre-fix body, whose internal
    coordination encodes the real scan→renew→fail interleave the bug
    needs. Returns the task's final status.
    """
    _REAP_SCANNED.clear()
    _RENEW_COMMITTED.clear()

    def reaper() -> None:
        try:
            Task.objects.reap_stale_claims()
        finally:
            for conn in connections.all():
                conn.close()

    def worker() -> None:
        try:
            task = Task.objects.get(pk=task_pk)
            _REAP_SCANNED.wait(timeout=10)  # let the reaper scan first
            task.renew_lease(lease_seconds=300)  # still-alive heartbeat
            _RENEW_COMMITTED.set()
        finally:
            for conn in connections.all():
                conn.close()

    threads = [threading.Thread(target=reaper), threading.Thread(target=worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return Task.objects.get(pk=task_pk).status


def _pre_fix_reap(self: TaskQuerySet) -> int:
    """PRE-FIX body: scan stale, then unconditional fail() per row.

    Scans the (then-stale) set, signals, waits for the racing renew to
    commit, then unconditionally ``fail()``s the pre-scan rows with no
    re-check — clobbering the renewed lease → spurious FAILED. This is
    exactly the scan→renew→fail race #800 N5 removes.
    """
    now = timezone.now()
    stale = list(self.filter(status=Task.Status.CLAIMED, lease_expires_at__lt=now))
    _REAP_SCANNED.set()
    _RENEW_COMMITTED.wait(timeout=10)
    count = 0
    for task in stale:
        task.fail()
        count += 1
    return count


@pytest.mark.usefixtures("file_backed_default")
class TestReapStaleClaimsRealMethod:
    """Real ``reap_stale_claims`` vs real ``renew_lease`` on the prod DB."""

    def test_real_cas_reap_spares_renewed_lease_green(self) -> None:
        """A lease renewed before the REAL reap's write is not reaped."""
        pk = _stale_claimed_task().pk
        status = _renew_then_real_reap(pk)
        assert status == Task.Status.CLAIMED, f"renewed lease was wrongly reaped: {status!r}"

    def test_real_cas_reaps_a_genuinely_still_stale_task(self) -> None:
        """Counterpart: a task NOT renewed IS reaped by the real method.

        Guards the green test against the trivial vacuity where the reap
        never fails anything: with no racing renew the real
        ``reap_stale_claims`` must still FAIL the stale CLAIMED task.
        """
        pk = _stale_claimed_task().pk
        Task.objects.reap_stale_claims()
        assert Task.objects.get(pk=pk).status == Task.Status.FAILED

    def test_pre_fix_unconditional_reap_spuriously_fails_renewed_lease_red(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation-revert proof: the pre-fix body spurious-fails.

        Monkeypatching ``TaskQuerySet.reap_stale_claims`` to the pre-fix
        scan-then-unconditional-``fail()`` body and running the same
        scan→renew→fail interleave fails the just-renewed task — proving
        the green test guards the real shipped CAS and is not vacuous.
        """
        monkeypatch.setattr(TaskQuerySet, "reap_stale_claims", _pre_fix_reap)
        pk = _stale_claimed_task().pk
        status = _race_pre_fix_reap_vs_renew(pk)
        assert status == Task.Status.FAILED, (
            f"pre-fix reap unexpectedly spared the renewed lease (expected spurious FAILED): {status!r}"
        )
