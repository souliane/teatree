"""teatree.loops.loop_staleness — the "is anything actually ticking?" reading.

The blind spot this closes: the worker holds the flock, ``loop_runner_enabled`` is
ON and every ``loop_timer`` row is READY, yet a mode mask admits no loop, so no
``Loop.last_run_at`` moves. The gate has to fire on that WITHOUT firing on the two
benign shapes it sits next to — a fleet that simply is not due, and one loop an
operator deliberately turned off. Integration-first against the real DB;
``iter_loops`` is stubbed to a small set so the assertions do not depend on the
seeded production loops.
"""

import datetime as dt
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.core.mode_resolution import ResolvedMode
from teatree.core.models import Loop, LoopState, Mode, ModeOverride, Prompt
from teatree.loops.base import MiniLoop
from teatree.loops.loop_staleness import (
    STALE_CADENCE_MULTIPLIER,
    Admission,
    LoopHealth,
    StaleLoop,
    admission,
    driverless_loops,
    format_age,
    loop_health,
    stale_loops,
)

# Every seam is a deferred (function-level) import in the module under test, so it is
# patched where it is DEFINED — patching a name on ``loop_staleness`` would miss it.
_REGISTRY_SEAM = "teatree.loops.registry.iter_loops"
_ADMITTED_SEAM = "teatree.loops.loop_table.admitted_loop_names"
_MODE_SEAM = "teatree.core.mode_resolution.resolve_active_mode"
_HOLDS_SEAM = "teatree.loop.loop_state_db.control_planes_in_db"


def _mini(name: str, *, off_live_tick: bool = False) -> MiniLoop:
    return MiniLoop(
        name=name,
        default_cadence_seconds=60,
        build_jobs=lambda **_: [],
        off_live_tick=off_live_tick,
    )


def _loop(
    name: str,
    *,
    cadence: int | None = 300,
    ran_ago: dt.timedelta | None = None,
    enabled: bool = True,
    colleague_facing: bool = False,
) -> Loop:
    # A ``Loop`` row must carry a prompt XOR a script (``loop_prompt_xor_script``).
    prompt, _ = Prompt.objects.get_or_create(name="demo-prompt", defaults={"body": "do x"})
    return Loop.objects.create(
        name=name,
        prompt=prompt,
        enabled=enabled,
        colleague_facing=colleague_facing,
        delay_seconds=cadence,
        last_run_at=None if ran_ago is None else timezone.now() - ran_ago,
    )


def _mode(name: str = "engaged", *, entries: dict[str, bool] | None = None, defers: bool = False) -> ResolvedMode:
    return ResolvedMode(
        mode=Mode(name=name, entries=entries or {}, defers_questions=defers),
        source="override",
        until=None,
    )


class _LoopTableCase(django.test.TestCase):
    """Start from an EMPTY loop table — the migrations seed the production set."""

    def setUp(self) -> None:
        super().setUp()
        Loop.objects.all().delete()


@django.test.override_settings(USE_TZ=True)
class TestStaleLoops(_LoopTableCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(patch(_MODE_SEAM, return_value=_mode()))
        self.enterContext(patch(_HOLDS_SEAM, return_value=(set(), {})))

    def test_fresh_anchor_is_not_stale(self) -> None:
        _loop("tickets", cadence=300, ran_ago=dt.timedelta(seconds=120))
        with patch(_REGISTRY_SEAM, return_value=(_mini("tickets"),)):
            assert stale_loops(timezone.now()) == []

    def test_anchor_past_the_multiplier_is_stale(self) -> None:
        _loop("tickets", cadence=300, ran_ago=dt.timedelta(hours=7))
        with patch(_REGISTRY_SEAM, return_value=(_mini("tickets"),)):
            stale = stale_loops(timezone.now())
        assert [loop.name for loop in stale] == ["tickets"]
        assert stale[0].cadence_seconds == 300
        assert stale[0].age_label.startswith("last ran 7h")

    def test_anchor_exactly_at_the_multiplier_is_not_yet_stale(self) -> None:
        # The boundary is strict (>), so a loop that has just reached 3x its cadence
        # still gets the benefit of the doubt rather than flapping on the edge. ``now``
        # is derived from the anchor, so the boundary is exact rather than wall-clock racy.
        row = _loop("tickets", cadence=300, ran_ago=dt.timedelta(seconds=1))
        boundary = row.last_run_at + dt.timedelta(seconds=STALE_CADENCE_MULTIPLIER * 300)
        with patch(_REGISTRY_SEAM, return_value=(_mini("tickets"),)):
            assert stale_loops(boundary) == []
            assert [loop.name for loop in stale_loops(boundary + dt.timedelta(seconds=1))] == ["tickets"]

    def test_freshly_seeded_never_run_loop_is_not_stale(self) -> None:
        # Every new install seeds its loops with no anchor and starts the worker after.
        # Flagging that would fail on day one, so a young never-run row is silent.
        _loop("tickets", cadence=300, ran_ago=None)
        with patch(_REGISTRY_SEAM, return_value=(_mini("tickets"),)):
            assert stale_loops(timezone.now()) == []

    def test_long_seeded_never_run_loop_is_stale(self) -> None:
        # ``created_at`` is the fallback anchor: a loop enabled for many cadences that
        # has still never run is frozen just as surely as one that stopped.
        row = _loop("tickets", cadence=300, ran_ago=None)
        Loop.objects.filter(pk=row.pk).update(created_at=timezone.now() - dt.timedelta(hours=7))
        with patch(_REGISTRY_SEAM, return_value=(_mini("tickets"),)):
            stale = stale_loops(timezone.now())
        assert [loop.ever_ran for loop in stale] == [False]
        assert stale[0].age_label.startswith("never run (seeded 7h")

    def test_disabled_loop_is_never_stale(self) -> None:
        _loop("tickets", cadence=300, ran_ago=dt.timedelta(hours=7), enabled=False)
        with patch(_REGISTRY_SEAM, return_value=(_mini("tickets"),)):
            assert stale_loops(timezone.now()) == []

    def test_off_live_tick_loop_is_never_stale(self) -> None:
        # ``dream`` runs off the live tick, so the live tick leaving its
        # anchor alone is correct, not a fault.
        _loop("dream", cadence=86400, ran_ago=dt.timedelta(days=30))
        with patch(_REGISTRY_SEAM, return_value=(_mini("dream", off_live_tick=True),)):
            assert stale_loops(timezone.now()) == []

    def test_cadence_less_loop_is_never_stale(self) -> None:
        _loop("every_tick", cadence=None, ran_ago=dt.timedelta(days=1))
        with patch(_REGISTRY_SEAM, return_value=(_mini("every_tick"),)):
            assert stale_loops(timezone.now()) == []

    def test_stale_loops_are_sorted_by_name(self) -> None:
        for name in ("ship", "dispatch", "tickets"):
            _loop(name, cadence=300, ran_ago=dt.timedelta(hours=7))
        registry = tuple(_mini(name) for name in ("ship", "dispatch", "tickets"))
        with patch(_REGISTRY_SEAM, return_value=registry):
            assert [loop.name for loop in stale_loops(timezone.now())] == ["dispatch", "ship", "tickets"]


@django.test.override_settings(USE_TZ=True)
class TestMeasuredSetIsTheAdmissionVerdict(_LoopTableCase):
    """Staleness measures whatever the verdict says should run — not the raw column (#4185).

    Real ``Mode`` / ``ModeOverride`` rows rather than a patched resolver seam: the whole
    point is that the effective verdict, not ``Loop.enabled``, decides the measured set.
    """

    def _activate(self, entries: dict[str, bool]) -> None:
        Mode.objects.create(name="preset-4185", entries=entries)
        ModeOverride.objects.set_override("preset-4185")

    def test_a_preset_forced_on_column_disabled_loop_is_measured_and_stale(self) -> None:
        _loop("tickets", cadence=300, ran_ago=dt.timedelta(hours=7), enabled=False)
        self._activate({"tickets": True})
        with patch(_REGISTRY_SEAM, return_value=(_mini("tickets"),)):
            stale = stale_loops(timezone.now())
        assert [loop.name for loop in stale] == ["tickets"]
        assert stale[0].suppressed is False

    def test_a_preset_masked_off_column_enabled_loop_is_not_measured(self) -> None:
        _loop("tickets", cadence=300, ran_ago=dt.timedelta(hours=7), enabled=True)
        self._activate({"tickets": False})
        with patch(_REGISTRY_SEAM, return_value=(_mini("tickets"),)):
            assert stale_loops(timezone.now()) == []

    def test_a_held_loop_is_not_measured(self) -> None:
        _loop("tickets", cadence=300, ran_ago=dt.timedelta(hours=7), enabled=True)
        LoopState.objects.pause("tickets")
        with patch(_REGISTRY_SEAM, return_value=(_mini("tickets"),)):
            assert stale_loops(timezone.now()) == []

    def test_admission_counts_the_admitted_total_not_the_enabled_column(self) -> None:
        _loop("tickets", cadence=300, ran_ago=dt.timedelta(seconds=60), enabled=False)
        self._activate({"tickets": True})
        with patch(_ADMITTED_SEAM, return_value=["tickets"]):
            verdict = admission(timezone.now())
        assert verdict.admitted_total == 1


@django.test.override_settings(USE_TZ=True)
class TestSuppressionClassification(_LoopTableCase):
    """Which deliberate control plane excuses an ADMITTED loop from standing still.

    Only the colleague gate: a masked or held loop is not admitted, so it is never
    measured and needs no excuse — that half is covered by
    :class:`TestMeasuredSetIsTheAdmissionVerdict`.
    """

    def _stale_one(self, **mode_kwargs: object) -> StaleLoop:
        with (
            patch(_REGISTRY_SEAM, return_value=(_mini("review"),)),
            patch(_MODE_SEAM, return_value=_mode(**mode_kwargs)),
        ):
            return stale_loops(timezone.now())[0]

    def test_colleague_loop_is_suppressed_while_questions_defer(self) -> None:
        _loop("review", ran_ago=dt.timedelta(hours=7), colleague_facing=True)
        assert self._stale_one(name="unattended", defers=True).suppressed

    def test_colleague_loop_is_not_suppressed_while_questions_are_live(self) -> None:
        _loop("review", ran_ago=dt.timedelta(hours=7), colleague_facing=True)
        assert not self._stale_one(name="engaged", defers=False).suppressed

    def test_unmasked_loop_is_not_suppressed(self) -> None:
        _loop("review", ran_ago=dt.timedelta(hours=7))
        assert not self._stale_one().suppressed


@django.test.override_settings(USE_TZ=True)
class TestLoopHealth(_LoopTableCase):
    def _health(self, *, admitted: list[str], mode: ResolvedMode, registry: tuple[MiniLoop, ...]) -> LoopHealth:
        with (
            patch(_REGISTRY_SEAM, return_value=registry),
            patch(_ADMITTED_SEAM, return_value=admitted),
            patch(_MODE_SEAM, return_value=mode),
            patch(_HOLDS_SEAM, return_value=(set(), {})),
        ):
            return loop_health(timezone.now())

    def test_health_is_ok_when_every_loop_advances(self) -> None:
        _loop("tickets", ran_ago=dt.timedelta(seconds=60))
        health = self._health(admitted=["tickets"], mode=_mode(), registry=(_mini("tickets"),))
        assert health.ok
        assert health.lines() == ["mode: engaged (source=override) — 1/1 admitted loop(s) due"]

    def test_healthy_fleet_that_is_simply_not_due_is_ok(self) -> None:
        # Admission requires ``is_due``, so a fleet that ticked a second ago admits
        # nothing. That is the normal case and must never read as a failure.
        _loop("tickets", ran_ago=dt.timedelta(seconds=10))
        health = self._health(admitted=[], mode=_mode(), registry=(_mini("tickets"),))
        assert health.ok
        assert "0/1 admitted loop(s) due" in "\n".join(health.lines())

    def test_admission_reports_the_resolved_mode_and_admitted_loops(self) -> None:
        # ``admission`` reads the SAME verdict the timer chain gates on — it must
        # report the resolved mode name/source and the admitted loop names.
        _loop("tickets", ran_ago=dt.timedelta(seconds=60))
        with (
            patch(_ADMITTED_SEAM, return_value=["tickets"]),
            patch(_MODE_SEAM, return_value=_mode()),
            patch(_HOLDS_SEAM, return_value=(set(), {})),
        ):
            verdict = admission(timezone.now())
        assert isinstance(verdict, Admission)
        assert verdict.mode == "engaged"
        assert verdict.source == "override"
        assert verdict.admitted == ("tickets",)
        assert verdict.admitted_total == 1

    def test_frozen_fleet_fails_and_names_the_mode(self) -> None:
        # The seven-hour incident: an all-off mask, forgotten, with a live worker.
        names = ("tickets", "dispatch", "ship")
        for name in names:
            _loop(name, ran_ago=dt.timedelta(hours=7))
        health = self._health(
            admitted=[],
            mode=_mode(name="offline", entries=dict.fromkeys(names, False), defers=True),
            registry=tuple(_mini(name) for name in names),
        )
        rendered = "\n".join(health.lines())
        assert not health.ok
        assert health.frozen_fleet
        assert "ticking NOTHING" in rendered
        assert "'offline'" in rendered
        assert "loop preset auto" in rendered

    def test_one_deliberately_suppressed_loop_does_not_fail(self) -> None:
        # The trust test: an operator who turned `review` off for the week must not be
        # told the factory is broken every time they check on it.
        _loop("tickets", ran_ago=dt.timedelta(seconds=30))
        _loop("review", ran_ago=dt.timedelta(hours=7), colleague_facing=True)
        health = self._health(
            admitted=["tickets"],
            mode=_mode(name="unattended", defers=True),
            registry=(_mini("tickets"), _mini("review")),
        )
        rendered = "\n".join(health.lines())
        assert health.ok
        assert not health.frozen_fleet
        assert "idle by configuration: review" in rendered
        assert "FAIL" not in rendered

    def test_unexplained_stale_loop_fails_even_beside_healthy_ones(self) -> None:
        _loop("tickets", ran_ago=dt.timedelta(seconds=30))
        _loop("dispatch", ran_ago=dt.timedelta(hours=7))
        health = self._health(
            admitted=["tickets"],
            mode=_mode(),
            registry=(_mini("tickets"), _mini("dispatch")),
        )
        rendered = "\n".join(health.lines())
        assert not health.ok
        assert not health.frozen_fleet
        assert "dispatch" in rendered
        assert "the colleague gate does not explain it" in rendered

    def test_json_carries_the_mode_and_every_stale_loop(self) -> None:
        _loop("tickets", ran_ago=dt.timedelta(hours=7))
        payload = self._health(
            admitted=[],
            mode=_mode(name="offline", entries={"tickets": False}),
            registry=(_mini("tickets"),),
        ).as_json()
        assert payload["mode"] == "offline"
        assert payload["mode_source"] == "override"
        assert payload["admitted"] == []
        assert payload["admitted_total"] == 1
        assert payload["considered"] == 1
        assert payload["frozen_fleet"] is True
        assert [entry["name"] for entry in payload["stale"]] == ["tickets"]

    def test_long_stale_list_is_truncated_with_a_tail_count(self) -> None:
        names = [f"loop{index:02d}" for index in range(12)]
        for name in names:
            _loop(name, ran_ago=dt.timedelta(hours=7))
        rendered = "\n".join(
            self._health(
                admitted=[],
                mode=_mode(name="offline", entries=dict.fromkeys(names, False)),
                registry=tuple(_mini(name) for name in names),
            ).lines()
        )
        assert "loop00" in rendered
        assert "loop11" not in rendered
        assert "... and 4 more" in rendered


class TestFormatAge(django.test.SimpleTestCase):
    def test_compact_human_age(self) -> None:
        cases = [(0, "0s"), (45, "45s"), (90, "1m"), (3599, "59m"), (3600, "1h"), (25200, "7h"), (172800, "2d")]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                assert format_age(seconds) == expected


class TestFrozenFleetPredicate(django.test.SimpleTestCase):
    @staticmethod
    def _stale(name: str, *, suppressed: bool) -> StaleLoop:
        return StaleLoop(name=name, cadence_seconds=300, age_seconds=25200, ever_ran=True, suppressed=suppressed)

    def test_a_box_with_no_measured_loops_is_not_frozen(self) -> None:
        # No loops to judge is idle by configuration, not a freeze — the alarm needs a
        # denominator before "all of them are behind" means anything.
        verdict = Admission(mode="offline", source="override", admitted=(), admitted_total=0)
        assert not LoopHealth(admission=verdict, stale=(), considered=0).frozen_fleet

    def test_all_suppressed_still_counts_as_a_frozen_fleet(self) -> None:
        # Precisely the incident: every loop off ON PURPOSE, and forgotten. Deliberate
        # does not mean fine once it is total.
        verdict = Admission(mode="offline", source="override", admitted=(), admitted_total=2)
        health = LoopHealth(
            admission=verdict,
            stale=(self._stale("tickets", suppressed=True), self._stale("ship", suppressed=True)),
            considered=2,
        )
        assert health.frozen_fleet
        assert not health.ok


class TestDriverlessLoops(django.test.SimpleTestCase):
    """The sibling alarm for the hole staleness structurally cannot see.

    ``_measured_loops`` keeps only rows that are enabled AND live-tick AND
    interval-cadenced, so a loop with NO driver at all — ``off_live_tick``, disabled,
    masked, or ``daily_at``-scheduled — is invisible to the alarm built to catch loops
    that are not ticking. Driverlessness is a WIRING property, so it is measured off the
    registry alone and is deliberately not gated on enablement or the mode mask.
    """

    def test_an_off_live_tick_loop_with_no_tick_command_is_driverless(self) -> None:
        registry = (_mini("live"), _mini("orphan", off_live_tick=True))
        with patch(_REGISTRY_SEAM, return_value=registry):
            assert driverless_loops() == ("orphan",)

    def test_an_off_live_tick_loop_that_declares_its_tick_command_has_a_driver(self) -> None:
        driven = MiniLoop(
            name="driven",
            default_cadence_seconds=60,
            build_jobs=lambda **_: [],
            off_live_tick=True,
            off_tick_command=("driven", "tick"),
        )
        with patch(_REGISTRY_SEAM, return_value=(driven,)):
            assert driverless_loops() == ()

    def test_a_live_tick_loop_is_never_driverless(self) -> None:
        # The loop-timer chain drives it; declaring a tick command would be meaningless.
        with patch(_REGISTRY_SEAM, return_value=(_mini("live"),)):
            assert driverless_loops() == ()

    def test_the_real_registry_leaves_no_loop_driverless(self) -> None:
        assert driverless_loops() == ()


class TestDriverlessHealthVerdict(django.test.SimpleTestCase):
    @staticmethod
    def _verdict() -> Admission:
        return Admission(mode="engaged", source="override", admitted=("tickets",), admitted_total=1)

    def test_a_driverless_loop_fails_health_even_with_nothing_stale(self) -> None:
        health = LoopHealth(admission=self._verdict(), stale=(), considered=1, driverless=("directive_loop",))
        assert not health.ok
        rendered = "\n".join(health.lines())
        assert "FAIL" in rendered
        assert "directive_loop" in rendered
        assert health.as_json()["driverless"] == ["directive_loop"]

    def test_no_driverless_loop_leaves_health_ok(self) -> None:
        health = LoopHealth(admission=self._verdict(), stale=(), considered=1)
        assert health.ok
        assert "FAIL" not in "\n".join(health.lines())
