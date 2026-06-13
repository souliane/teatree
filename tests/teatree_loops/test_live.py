"""``teatree.loops.live`` — the shared live loop-status snapshot (#1744).

Unit coverage for the edge cases the management-command integration test does
not exercise directly: the stall predicate when nothing has ever ticked, the
PID-anchored owner liveness branches, and the entry-level age / overdue / due
helpers. The clock is pinned so the derived numbers are deterministic.
"""

import datetime as dt
import os
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.models.loop_lease import LoopLease
from teatree.core.models.mini_loop_marker import MiniLoopMarker
from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopsConfig
from teatree.loops.live import STALL_FACTOR, LoopKind, LoopStatusEntry, build_report, owned_per_loop_owners

_LIVE_PID = os.getpid()
_DEAD_PID = 2_000_000_000


def _stub_loop(name: str, cadence: int) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=cadence, build_jobs=lambda **_: [])


@contextmanager
def _registry(*loops: MiniLoop) -> Iterator[None]:
    with (
        patch("teatree.loops.live.iter_loops", return_value=loops),
        patch.object(LoopsConfig, "load", classmethod(lambda cls, path=None: cls())),
    ):
        yield


class TestEntryHelpers:
    def _entry(self, *, last: dt.datetime | None, nxt: dt.datetime | None) -> LoopStatusEntry:
        return LoopStatusEntry(
            name="x",
            kind=LoopKind.MINI,
            enabled=True,
            cadence_seconds=60,
            last_fired_at=last,
            next_fire_at=nxt,
        )

    def test_never_fired_entry(self) -> None:
        now = timezone.now()
        entry = self._entry(last=None, nxt=None)
        assert entry.never_fired is True
        assert entry.age_seconds(now) is None
        assert entry.overdue(now) is False
        assert entry.due_seconds(now) is None

    def test_overdue_when_next_fire_in_past(self) -> None:
        now = timezone.now()
        entry = self._entry(last=now - dt.timedelta(seconds=120), nxt=now - dt.timedelta(seconds=60))
        assert entry.overdue(now) is True
        assert entry.due_seconds(now) == -60
        assert entry.age_seconds(now) == 120

    def test_future_next_fire_not_overdue(self) -> None:
        now = timezone.now()
        entry = self._entry(last=now, nxt=now + dt.timedelta(seconds=30))
        assert entry.overdue(now) is False
        assert entry.due_seconds(now) == 30


@django.test.override_settings(USE_TZ=True)
class TestStallPredicate(django.test.TestCase):
    def test_stalled_when_no_tick_ever(self) -> None:
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report()
        assert report.last_tick_at is None
        assert report.last_tick_age_seconds is None
        assert report.stalled is True

    def test_stalled_when_oldest_beyond_factor(self) -> None:
        now = timezone.now()
        cadence = 720
        MiniLoopMarker.objects.mark_fired("dispatch", now - dt.timedelta(seconds=STALL_FACTOR * cadence + 5))
        with _registry(_stub_loop("dispatch", 300)), patch("teatree.loops.live.cadence_for_loop", return_value=cadence):
            report = build_report(now=now)
        assert report.stalled is True

    def test_not_stalled_when_recent(self) -> None:
        now = timezone.now()
        MiniLoopMarker.objects.mark_fired("dispatch", now - dt.timedelta(seconds=10))
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        assert report.stalled is False


@django.test.override_settings(USE_TZ=True)
class TestOwnerLiveness(django.test.TestCase):
    def test_alive_pid_is_live_even_past_ttl(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name="loop-owner",
            session_id="busy",
            owner_pid=_LIVE_PID,
            lease_expires_at=now - dt.timedelta(hours=1),
        )
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        assert report.owner.pid_is_alive is True
        assert report.owner.is_live is True

    def test_unexpired_ttl_is_live_even_with_dead_pid(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name="loop-owner",
            session_id="fresh",
            owner_pid=_DEAD_PID,
            lease_expires_at=now + dt.timedelta(minutes=30),
        )
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        assert report.owner.pid_is_alive is False
        assert report.owner.is_live is True

    def test_dead_pid_and_expired_ttl_is_not_live(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name="loop-owner",
            session_id="gone",
            owner_pid=_DEAD_PID,
            lease_expires_at=now - dt.timedelta(hours=1),
        )
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        assert report.owner.is_live is False
        assert report.owner.is_claimed is True

    def test_null_pid_owner_decided_by_ttl(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name="loop-owner",
            session_id="nopid",
            owner_pid=None,
            lease_expires_at=now - dt.timedelta(hours=1),
        )
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        assert report.owner.pid_is_alive is False
        assert report.owner.is_live is False

    def test_no_lease_is_unclaimed(self) -> None:
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report()
        assert report.owner.is_claimed is False
        assert report.owner.is_live is False


@django.test.override_settings(USE_TZ=True)
class TestPerLoopOwners(django.test.TestCase):
    """The additive per-loop owning-session health layer (#1834).

    ``build_report`` surfaces one :class:`LoopOwnerStatus` per ``loop:<name>``
    lease, disjoint from the global ``loop-owner`` row, with the same
    pid-anchored liveness. Empty under the single-owner default.
    """

    def test_no_per_loop_leases_means_empty(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(name="loop-owner", session_id="global", owner_pid=_LIVE_PID, lease_expires_at=now)
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        assert report.per_loop_owners == ()

    def test_two_per_loop_owners_surfaced_sorted_by_slot(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name="loop:review",
            session_id="sess-review",
            owner_pid=_LIVE_PID,
            lease_expires_at=now + dt.timedelta(minutes=30),
        )
        LoopLease.objects.create(
            name="loop:dispatch",
            session_id="sess-dispatch",
            owner_pid=_LIVE_PID,
            lease_expires_at=now + dt.timedelta(minutes=30),
        )
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        assert [o.slot for o in report.per_loop_owners] == ["loop:dispatch", "loop:review"]
        assert report.per_loop_owners[0].session_id == "sess-dispatch"
        assert all(o.is_live for o in report.per_loop_owners)

    def test_per_loop_owner_pid_liveness_is_anchored(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name="loop:dispatch",
            session_id="busy",
            owner_pid=_LIVE_PID,
            lease_expires_at=now - dt.timedelta(hours=1),
        )
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        owner = report.per_loop_owners[0]
        assert owner.pid_is_alive is True
        assert owner.is_live is True

    def test_global_owner_row_is_not_a_per_loop_owner(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(name="loop-owner", session_id="g", owner_pid=_LIVE_PID, lease_expires_at=now)
        # The infra-slot leases use ``-`` not ``:`` so they are also excluded.
        LoopLease.objects.create(name="loop-tick", owner="t", acquired_at=now)
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        assert report.per_loop_owners == ()
        assert report.owner.slot == "loop-owner"


@django.test.override_settings(USE_TZ=True)
class TestOwnedPerLoopOwners(django.test.TestCase):
    """``owned_per_loop_owners`` scopes the per-loop layer to one session (#1834 WI-2).

    The default ``t3 loop list`` / statusline view subtracts every per-loop
    owner NOT owned by the current session; ``--all`` keeps the full
    cross-session set. An empty ``session_id`` (cron / anonymous) fails open
    to the full set so the default view is never blanked.
    """

    def _seed(self, now: dt.datetime) -> None:
        LoopLease.objects.create(
            name="loop:dispatch",
            session_id="sess-A",
            owner_pid=_LIVE_PID,
            lease_expires_at=now + dt.timedelta(minutes=30),
        )
        LoopLease.objects.create(
            name="loop:review",
            session_id="sess-B",
            owner_pid=_LIVE_PID,
            lease_expires_at=now + dt.timedelta(minutes=30),
        )

    def test_scopes_to_owning_session(self) -> None:
        now = timezone.now()
        self._seed(now)
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        owned = owned_per_loop_owners(report, "sess-A")
        assert [o.slot for o in owned] == ["loop:dispatch"]
        assert owned[0].session_id == "sess-A"

    def test_other_session_subtracted_but_present_in_full_set(self) -> None:
        now = timezone.now()
        self._seed(now)
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        # The default view for session A excludes B's loop ...
        assert [o.slot for o in owned_per_loop_owners(report, "sess-A")] == ["loop:dispatch"]
        # ... yet B's loop genuinely exists in the unfiltered cross-session set.
        assert {o.slot for o in report.per_loop_owners} == {"loop:dispatch", "loop:review"}

    def test_empty_session_fails_open_to_full_set(self) -> None:
        now = timezone.now()
        self._seed(now)
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        owned = owned_per_loop_owners(report, "")
        assert {o.slot for o in owned} == {"loop:dispatch", "loop:review"}

    def test_no_per_loop_rows_is_empty_for_any_session(self) -> None:
        now = timezone.now()
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        assert owned_per_loop_owners(report, "sess-A") == ()
        assert owned_per_loop_owners(report, "") == ()


@django.test.override_settings(USE_TZ=True)
class TestInfraEntries(django.test.TestCase):
    def test_held_lease_marked_held(self) -> None:
        now = timezone.now()
        LoopLease.objects.create(
            name="loop-tick",
            owner="holder",
            acquired_at=now,
            lease_expires_at=now + dt.timedelta(minutes=2),
        )
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report(now=now)
        tick = next(e for e in report.infra_slots if e.name == "loop-tick")
        assert tick.held is True
        assert tick.kind is LoopKind.INFRA
        assert tick.next_fire_at is not None

    def test_missing_lease_row_is_idle_never_fired(self) -> None:
        with _registry(_stub_loop("dispatch", 300)):
            report = build_report()
        tick = next(e for e in report.infra_slots if e.name == "loop-tick")
        assert tick.held is False
        assert tick.never_fired is True
