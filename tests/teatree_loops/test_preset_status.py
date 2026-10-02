"""teatree.loops.preset_status — the preset/mode observability rendering (#3159).

The active-preset summary and the ``schedule:`` / ``mode:`` / ``forced ON/OFF:``
statusline handles. The per-loop effective verdict they render lives in
``teatree.loops.enable_verdict`` (see ``test_enable_verdict.py``) — the one seam the
tick's own admission also reads.
"""

import datetime as dt

import django.test

from teatree.core.models import ConfigSetting, Loop, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.preset_status import (
    active_summary,
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
class TestActiveSummary(django.test.TestCase):
    def test_summary_reports_active_preset(self) -> None:
        Mode.objects.update_or_create(name="maintenance", defaults={"entries": {}})
        ModeOverride.objects.set_override("maintenance", reason="test override")
        summary = active_summary()
        assert summary is not None
        assert summary.name == "maintenance"
        assert summary.layer == "override"

    def test_summary_none_when_no_preset(self) -> None:
        assert active_summary() is None


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestStatuslineChunk(django.test.TestCase):
    def test_default_mode_when_nothing_governs(self) -> None:
        # Post-merge there is ALWAYS a resolved mode; a quiet machine reads the
        # configured default (``present``) rather than an empty handle.
        assert statusline_chunk() == "mode: present"

    def test_manual_override_reads_mode_manual(self) -> None:
        # A manual override (#3494, #61) reads ``mode: manual`` — the layer, not
        # the mode name — so the operator sees the schedule is not governing.
        Mode.objects.update_or_create(name="maintenance", defaults={"entries": {}})
        ModeOverride.objects.set_override("maintenance", reason="test override")
        assert statusline_chunk() == "mode: manual"

    def test_a_manual_override_reads_as_manual(self) -> None:
        Mode.objects.update_or_create(name="maintenance", defaults={"entries": {}})
        ModeOverride.objects.set_override("maintenance", reason="test override")
        assert statusline_chunk() == "mode: manual"


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestScheduleAndOverrideChunks(django.test.TestCase):
    def test_schedule_chunk_names_the_active_schedule(self) -> None:

        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")
        assert schedule_chunk() == "schedule: standard"

    def test_schedule_chunk_reads_none_active_without_active_schedule(self) -> None:

        assert schedule_chunk() == "schedule: none active"

    def test_manual_override_entries_only_divergent_forced_loops(self) -> None:

        _loop("ov-review")
        _loop("ov-news")
        Mode.objects.update_or_create(name="present", defaults={"entries": {"ov-review": True, "ov-news": False}})
        ModeOverride.objects.set_override("present", reason="test override")
        # review forced OFF and news forced ON both diverge from the preset's own opinion.
        Loop.objects.set_manual_override("ov-review", runs=False, reason="test override")
        Loop.objects.set_manual_override("ov-news", runs=True, reason="test override")
        assert manual_override_entries() == [("ov-news", True), ("ov-review", False)]

    def test_manual_override_entries_excludes_non_divergent(self) -> None:

        _loop("ov-same")
        Mode.objects.update_or_create(name="present", defaults={"entries": {"ov-same": True}})
        ModeOverride.objects.set_override("present", reason="test override")
        # Forced ON matches what the preset already says — not a divergence, so omitted.
        Loop.objects.set_manual_override("ov-same", runs=True, reason="test override")
        assert manual_override_entries() == []

    def test_manual_override_chunk_spells_out_forced_state(self) -> None:

        _loop("ov-a")
        _loop("ov-b")
        Mode.objects.update_or_create(name="present", defaults={"entries": {"ov-a": True, "ov-b": True}})
        ModeOverride.objects.set_override("present", reason="test override")
        Loop.objects.set_manual_override("ov-a", runs=False, reason="test override")
        Loop.objects.set_manual_override("ov-b", runs=True, reason="test override")
        # ov-b forced-on matches the preset → not divergent; only ov-a (forced OFF) shows.
        assert manual_override_chunk() == "forced OFF: ov-a"

    def test_manual_override_chunk_groups_on_and_off(self) -> None:

        _loop("ov-on")
        _loop("ov-off")
        Mode.objects.update_or_create(name="present", defaults={"entries": {"ov-on": False, "ov-off": True}})
        ModeOverride.objects.set_override("present", reason="test override")
        Loop.objects.set_manual_override("ov-on", runs=True, reason="test override")  # diverges from the preset's OFF
        Loop.objects.set_manual_override("ov-off", runs=False, reason="test override")  # diverges from the preset's ON
        assert manual_override_chunk() == "forced ON: ov-on · forced OFF: ov-off"


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestPresetLineChunk(django.test.TestCase):
    def test_shows_schedule_and_default_mode_when_nothing_governs(self) -> None:
        # The schedule handle is always spelled out and the mode handle is always
        # present (the configured default), so a quiet machine reads both.
        assert preset_line_chunk() == "schedule: none active · mode: present"

    def test_preset_line_handles_resolves_the_three_handles(self) -> None:
        _loop("plh-review")
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")
        Mode.objects.update_or_create(name="maintenance", defaults={"entries": {"plh-review": True}})
        ModeOverride.objects.set_override("maintenance", reason="test override")
        Loop.objects.set_manual_override("plh-review", runs=False, reason="test override")
        handles = preset_line_handles()
        assert handles.schedule == "schedule: standard"
        assert handles.mode == "mode: manual"
        assert handles.override == "forced OFF: plh-review"

    def test_preset_line_handles_quiet_machine_shows_schedule_and_default_mode(self) -> None:
        handles = preset_line_handles()
        assert handles.schedule == "schedule: none active"
        assert handles.mode == "mode: present"
        assert handles.override == ""

    def test_composes_schedule_mode_and_overrides(self) -> None:
        _loop("pl-review")
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")
        Mode.objects.update_or_create(name="maintenance", defaults={"entries": {"pl-review": True}})
        ModeOverride.objects.set_override("maintenance", reason="test override")
        Loop.objects.set_manual_override("pl-review", runs=False, reason="test override")
        chunk = preset_line_chunk()
        assert chunk == "schedule: standard · mode: manual · forced OFF: pl-review"

    def test_schedule_governed_names_the_mode_not_manual(self) -> None:
        Mode.objects.update_or_create(name="present", defaults={"entries": {}})
        schedule = ModeSchedule.objects.create(name="standard", timezone="UTC")
        ModeScheduleSlot.objects.create(
            schedule=schedule, days=[0, 1, 2, 3, 4, 5, 6], start_time=dt.time(0, 0), preset_name="present"
        )
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")
        chunk = preset_line_chunk()
        # Schedule-governed → the mode is named (not "manual"); no ⚠ marker.
        assert chunk.startswith("schedule: standard · mode: present")
        assert "⚠" not in chunk
        assert "manual" not in chunk
