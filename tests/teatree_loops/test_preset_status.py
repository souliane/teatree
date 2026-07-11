"""teatree.loops.preset_status — the shared effective-verdict surface (#3159).

One source of truth for ``preset show``, ``loops list``, and the statusline: the
active-preset summary, the per-loop effective verdict + deciding layer, and the
statusline chunk. Deciding layer mirrors the resolution order (hold > override/
schedule > base).
"""

import datetime as dt

import django.test
from django.utils import timezone

from teatree.core.models import Loop, LoopPreset, LoopPresetOverride, LoopState
from teatree.loops.preset_status import active_summary, effective_verdicts, statusline_chunk


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

    def test_names_the_active_preset(self) -> None:
        LoopPreset.objects.create(name="heads-down", entries={})
        LoopPresetOverride.objects.set_override("heads-down")
        assert statusline_chunk() == "preset heads-down"

    def test_includes_the_boundary_when_bounded(self) -> None:
        LoopPreset.objects.create(name="heads-down", entries={})
        until = timezone.now() + dt.timedelta(hours=3)
        LoopPresetOverride.objects.create(preset_name="heads-down", until=until)
        chunk = statusline_chunk()
        assert chunk.startswith("preset heads-down →")
