"""teatree.loops.master_lease_contradiction — unheld lease vs. ticking loops (#4253).

The two facts the skip message collapsed into one. Integration-first against the real
``Loop`` / ``LoopLease`` rows, because the finding IS a join of those two tables and a
stubbed read would prove nothing about the shape that produced the ticket: five loops
inside their own cadence while ``t3-master`` read unheld.

The seeded fleet ships with ``last_run_at`` unset, so it contributes no evidence — a
baseline assertion in each class pins that, so a future seed that DOES tick fails loudly
here rather than silently satisfying every arm below.
"""

import datetime as dt

import django.test
from django.utils import timezone

from teatree.core.loop_lease_manager import T3_MASTER_SLOT
from teatree.core.models import Loop, LoopLease, Prompt
from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID
from teatree.loops.master_lease_contradiction import (
    LIVE_TICK_CEILING_SECONDS,
    TICK_FRESHNESS_MULTIPLE,
    UnheldMasterLease,
    ticking_interval_loops,
    unheld_master_lease_with_live_ticks,
)

REVIEW = "t4253-review"
SHIP = "t4253-ship"
STANDUP = "t4253-standup"


def _loop(name: str, *, delay_seconds: int | None, last_run_at: dt.datetime | None, enabled: bool = True) -> Loop:
    prompt = Prompt.objects.create(name=f"prompt-{name}", body=f"run {name}")
    return Loop.objects.create(
        name=name, prompt=prompt, enabled=enabled, delay_seconds=delay_seconds, last_run_at=last_run_at
    )


class TestTickingIntervalLoops(django.test.TestCase):
    def setUp(self) -> None:
        self.now = timezone.now()
        assert ticking_interval_loops(self.now) == (), "the seeded fleet must contribute no ticks"

    def test_a_loop_inside_its_own_cadence_is_evidence_of_a_live_tick(self) -> None:
        _loop(REVIEW, delay_seconds=60, last_run_at=self.now - dt.timedelta(seconds=30))

        assert [name for name, _ in ticking_interval_loops(self.now)] == [REVIEW]

    def test_a_loop_overrun_past_the_freshness_multiple_is_not(self) -> None:
        stale = self.now - dt.timedelta(seconds=60 * TICK_FRESHNESS_MULTIPLE + 1)
        _loop(REVIEW, delay_seconds=60, last_run_at=stale)

        assert ticking_interval_loops(self.now) == ()

    def test_a_disabled_loop_is_never_evidence(self) -> None:
        _loop(REVIEW, delay_seconds=60, last_run_at=self.now, enabled=False)

        assert ticking_interval_loops(self.now) == ()

    def test_a_never_run_loop_is_never_evidence(self) -> None:
        _loop(REVIEW, delay_seconds=60, last_run_at=None)

        assert ticking_interval_loops(self.now) == ()

    def test_a_cron_loop_is_not_evidence_however_recent(self) -> None:
        # A daily loop's anchor says nothing about whether ticks are being driven NOW,
        # so only interval loops count — a fresh cron anchor must not mask an idle box.
        _loop(STANDUP, delay_seconds=None, last_run_at=self.now)

        assert ticking_interval_loops(self.now) == ()

    def test_the_age_travels_with_the_name(self) -> None:
        _loop(REVIEW, delay_seconds=600, last_run_at=self.now - dt.timedelta(seconds=45))

        ((name, age),) = ticking_interval_loops(self.now)

        assert name == REVIEW
        assert 44 <= age <= 46

    def test_a_daily_interval_loops_hours_old_anchor_is_not_evidence(self) -> None:
        # The measured shape on a real box: five enabled interval loops, freshest anchor
        # 5.9 HOURS old, every one inside 2x its own multi-hour cadence — so the finding
        # fired and read "ticking on cadence ... freshest 21306s ago" about an idle box.
        # A daily loop on schedule says nothing about whether ticks are driven RIGHT NOW,
        # which is the only claim this evidence is allowed to make.
        _loop(STANDUP, delay_seconds=86_400, last_run_at=self.now - dt.timedelta(hours=20))

        assert ticking_interval_loops(self.now) == ()

    def test_evidence_is_capped_by_wall_clock_not_only_by_cadence(self) -> None:
        # Just past the absolute ceiling but far inside 2x cadence: cadence alone would
        # admit it, so this pins that the ceiling is a SECOND, independent condition.
        stale = self.now - dt.timedelta(seconds=LIVE_TICK_CEILING_SECONDS + 60)
        _loop(REVIEW, delay_seconds=LIVE_TICK_CEILING_SECONDS * 4, last_run_at=stale)

        assert ticking_interval_loops(self.now) == ()

    def test_a_long_cadence_loop_that_ticked_moments_ago_is_still_evidence(self) -> None:
        # The ceiling must not throw away a genuine live tick: a slow loop that fired
        # seconds ago is exactly the "something is driving ticks now" signal.
        _loop(REVIEW, delay_seconds=86_400, last_run_at=self.now - dt.timedelta(seconds=5))

        assert [name for name, _ in ticking_interval_loops(self.now)] == [REVIEW]


class TestUnheldMasterLeaseWithLiveTicks(django.test.TestCase):
    """The exact box the ticket was filed from: worker driving ticks, lease reading unheld."""

    def setUp(self) -> None:
        self.now = timezone.now()
        assert unheld_master_lease_with_live_ticks(self.now) is None, "an untouched fleet is not the finding"

    def _tick(self, name: str) -> None:
        _loop(name, delay_seconds=60, last_run_at=self.now - dt.timedelta(seconds=5))

    def _hold_the_lease(self) -> None:
        LoopLease.objects.claim_ownership(T3_MASTER_SLOT, session_id=LOOP_RUNNER_SESSION_ID, owner_pid=None)

    def test_an_unheld_lease_with_loops_ticking_is_the_finding(self) -> None:
        self._tick(REVIEW)
        self._tick(SHIP)

        finding = unheld_master_lease_with_live_ticks(self.now)

        assert finding is not None
        assert finding.ticking_loops == (REVIEW, SHIP)
        assert finding.freshest_tick_seconds < 10

    def test_a_held_lease_is_not_a_finding(self) -> None:
        self._tick(REVIEW)
        self._hold_the_lease()

        assert unheld_master_lease_with_live_ticks(self.now) is None

    def test_an_unheld_lease_on_an_idle_box_is_not_a_finding(self) -> None:
        # An honestly idle box: nothing ticking, nobody owning. A stopped chain belongs
        # to the schedule-liveness check, not to this one.
        assert unheld_master_lease_with_live_ticks(self.now) is None

    def test_a_quiet_box_of_slow_loops_on_schedule_is_not_a_finding(self) -> None:
        # Reproduced live: a real box whose enabled loops are all multi-hour cadences,
        # freshest anchor ~6h old. Every one is inside 2x its own cadence, so a
        # cadence-only reading called it "ticking on cadence" and hard-FAILed the doctor
        # on a box where nothing had driven a tick since the small hours.
        for name, delay in ((REVIEW, 21_600), (SHIP, 43_200), (STANDUP, 86_400)):
            _loop(name, delay_seconds=delay, last_run_at=self.now - dt.timedelta(hours=6))

        assert unheld_master_lease_with_live_ticks(self.now) is None

    def test_a_released_lease_reopens_the_finding(self) -> None:
        self._tick(REVIEW)
        self._hold_the_lease()
        LoopLease.objects.release_ownership(T3_MASTER_SLOT, session_id=LOOP_RUNNER_SESSION_ID)

        assert unheld_master_lease_with_live_ticks(self.now) is not None


class TestDescribe:
    def test_it_names_the_ticking_loops_and_the_freshest_age(self) -> None:
        described = UnheldMasterLease(ticking_loops=("review", "ship"), freshest_tick_seconds=6.2).describe()

        assert "2 loop(s)" in described
        assert "review, ship" in described
        assert "freshest 6s ago" in described

    def test_a_long_list_is_summarised_rather_than_dumped(self) -> None:
        described = UnheldMasterLease(
            ticking_loops=tuple(f"l{i}" for i in range(8)), freshest_tick_seconds=1.0
        ).describe()

        assert "and 3 more" in described
        assert "l7" not in described

    def test_exactly_the_named_count_carries_no_more_suffix(self) -> None:
        described = UnheldMasterLease(
            ticking_loops=tuple(f"l{i}" for i in range(5)), freshest_tick_seconds=1.0
        ).describe()

        assert "more" not in described
