"""teatree.loops.preset_status — the shared effective-verdict surface (#3159).

One source of truth for ``preset show``, ``loops list``, and the statusline: the
active-preset summary, the per-loop effective verdict + deciding layer, and the
statusline chunk. Deciding layer mirrors the resolution order (hold > override/
schedule > base).
"""

import datetime as dt

import django.test
from django.utils import timezone

from teatree.core.models import (
    ConfigSetting,
    Loop,
    LoopPreset,
    LoopPresetOverride,
    LoopSchedule,
    LoopScheduleSlot,
    LoopState,
)
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.preset_status import (
    active_summary,
    effective_verdicts,
    manual_override_chunk,
    manual_override_entries,
    preset_line_chunk,
    preset_line_handles,
    schedule_chunk,
    statusline_chunk,
)


def _loop(name: str, *, enabled: bool = True) -> Loop:
    return Loop.objects.create(name=name, delay_seconds=60, script=f"src/teatree/loops/{name}/loop.py", enabled=enabled)


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestEffectiveVerdicts(django.test.TestCase):
    def test_base_layer_when_no_preset(self) -> None:
        _loop("ps-inbox")
        verdicts = {v.name: v for v in effective_verdicts()}
        assert verdicts["ps-inbox"].layer == "base"
        assert verdicts["ps-inbox"].admitted is True

    def test_hold_layer_wins_over_preset(self) -> None:
        _loop("ps-review")
        LoopState.objects.pause("ps-review")
        LoopPreset.objects.create(name="engaged", entries={"ps-review": True})
        LoopPresetOverride.objects.set_override("engaged")
        verdicts = {v.name: v for v in effective_verdicts()}
        assert verdicts["ps-review"].layer == "hold"
        assert verdicts["ps-review"].admitted is False

    def test_override_masks_a_loop_off(self) -> None:
        _loop("ps-review2")
        LoopPreset.objects.create(name="heads-down", entries={"ps-review2": False})
        LoopPresetOverride.objects.set_override("heads-down")
        verdicts = {v.name: v for v in effective_verdicts()}
        assert verdicts["ps-review2"].layer == "override"
        assert verdicts["ps-review2"].admitted is False

    def test_summary_reports_active_preset(self) -> None:
        LoopPreset.objects.create(name="heads-down", entries={})
        LoopPresetOverride.objects.set_override("heads-down")
        summary = active_summary()
        assert summary is not None
        assert summary.name == "heads-down"
        assert summary.layer == "override"

    def test_summary_none_when_no_preset(self) -> None:
        assert active_summary() is None


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestStatuslineChunk(django.test.TestCase):
    def test_empty_when_no_preset(self) -> None:
        assert statusline_chunk() == ""

    def test_manual_override_reads_preset_manual(self) -> None:
        # A manually-overridden preset (#3494) reads ``preset: manual`` — the
        # layer, not the preset name — so the operator sees the schedule is not
        # the one governing.
        LoopPreset.objects.create(name="heads-down", entries={})
        LoopPresetOverride.objects.set_override("heads-down")
        assert statusline_chunk() == "preset: manual"

    def test_manual_override_includes_the_boundary_when_bounded(self) -> None:
        LoopPreset.objects.create(name="heads-down", entries={})
        until = timezone.now() + dt.timedelta(hours=3)
        LoopPresetOverride.objects.create(preset_name="heads-down", until=until)
        chunk = statusline_chunk()
        assert chunk.startswith("preset: manual →")


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestScheduleAndOverrideChunks(django.test.TestCase):
    def test_schedule_chunk_names_the_active_schedule(self) -> None:

        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")
        assert schedule_chunk() == "schedule: standard"

    def test_schedule_chunk_reads_none_active_without_active_schedule(self) -> None:

        assert schedule_chunk() == "schedule: none active"

    def test_manual_override_entries_only_divergent_forced_loops(self) -> None:

        _loop("ov-review", enabled=True)
        _loop("ov-news", enabled=True)
        LoopPreset.objects.create(name="engaged", entries={"ov-news": False})
        LoopPresetOverride.objects.set_override("engaged")
        # review forced OFF (diverges from base ON); news forced ON (diverges
        # from the preset's OFF).
        LoopState.objects.override("ov-review", on=False)
        LoopState.objects.override("ov-news", on=True)
        assert manual_override_entries() == [("ov-news", True), ("ov-review", False)]

    def test_manual_override_entries_excludes_non_divergent(self) -> None:

        _loop("ov-same", enabled=True)
        # Forced ON matches the base ENABLED — not a divergence, so omitted.
        LoopState.objects.override("ov-same", on=True)
        assert manual_override_entries() == []

    def test_manual_override_chunk_spells_out_forced_state(self) -> None:

        _loop("ov-a", enabled=True)
        _loop("ov-b", enabled=True)
        LoopState.objects.override("ov-a", on=False)
        LoopState.objects.override("ov-b", on=True)
        # ov-b forced-on matches base → not divergent; only ov-a (forced OFF) shows.
        assert manual_override_chunk() == "forced OFF: ov-a"

    def test_manual_override_chunk_groups_on_and_off(self) -> None:

        _loop("ov-on", enabled=False)
        _loop("ov-off", enabled=True)
        LoopState.objects.override("ov-on", on=True)  # diverges from base OFF
        LoopState.objects.override("ov-off", on=False)  # diverges from base ON
        assert manual_override_chunk() == "forced ON: ov-on · forced OFF: ov-off"


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestPresetLineChunk(django.test.TestCase):
    def test_shows_schedule_none_active_when_nothing_governs(self) -> None:
        # The schedule handle is always spelled out, so the bundled view is never
        # empty — it reads ``schedule: none active`` on a quiet machine.
        assert preset_line_chunk() == "schedule: none active"

    def test_preset_line_handles_resolves_the_three_handles(self) -> None:
        _loop("plh-review", enabled=True)
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")
        LoopPreset.objects.create(name="heads-down", entries={})
        LoopPresetOverride.objects.set_override("heads-down")
        LoopState.objects.override("plh-review", on=False)
        handles = preset_line_handles()
        assert handles.schedule == "schedule: standard"
        assert handles.preset == "preset: manual"
        assert handles.override == "forced OFF: plh-review"

    def test_preset_line_handles_quiet_machine_shows_only_schedule(self) -> None:
        handles = preset_line_handles()
        assert handles.schedule == "schedule: none active"
        assert handles.preset == ""
        assert handles.override == ""

    def test_composes_schedule_preset_and_overrides(self) -> None:

        _loop("pl-review", enabled=True)
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")
        LoopPreset.objects.create(name="heads-down", entries={})
        LoopPresetOverride.objects.set_override("heads-down")
        LoopState.objects.override("pl-review", on=False)
        chunk = preset_line_chunk()
        assert chunk == "schedule: standard · preset: manual · forced OFF: pl-review"

    def test_schedule_governed_has_no_manual_marker(self) -> None:

        LoopPreset.objects.create(name="engaged", entries={})
        schedule = LoopSchedule.objects.create(name="standard", timezone="UTC")
        LoopScheduleSlot.objects.create(
            schedule=schedule, days=[0, 1, 2, 3, 4, 5, 6], start_time=dt.time(0, 0), preset_name="engaged"
        )
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")
        chunk = preset_line_chunk()
        # Schedule-governed → the preset is named (not "manual"); no ⚠ marker.
        assert chunk.startswith("schedule: standard · preset: engaged")
        assert "⚠" not in chunk
        assert "manual" not in chunk
