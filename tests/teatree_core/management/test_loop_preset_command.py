"""``manage.py loop_preset`` — create/edit/use/auto/show/list against a real DB.

Integration-first: drives the management command via ``call_command`` and asserts
the DB effect (the override row, the preset row) plus the rendered WHY output.
"""

import io
import json

import django.test
import pytest
from django.core.management import call_command

from teatree.core.models import PIN_MODES, Loop, LoopPreset, LoopPresetOverride


def _run(*args: str, **kwargs: object) -> str:
    out = io.StringIO()
    call_command("loop_preset", *args, stdout=out, **kwargs)
    return out.getvalue()


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLoopPresetCommand(django.test.TestCase):
    def _loop(self, name: str, *, enabled: bool = True) -> Loop:
        return Loop.objects.create(
            name=name, delay_seconds=60, script=f"src/teatree/loops/{name}/loop.py", enabled=enabled
        )

    def test_create_then_list_marks_entries(self) -> None:
        _run("create", "heads-down", "--set", "review=off", "--set", "dispatch=on", "--description", "deep work")
        preset = LoopPreset.objects.get(name="heads-down")
        assert preset.entries == {"review": False, "dispatch": True}
        assert preset.description == "deep work"
        listing = _run("list")
        assert "heads-down" in listing

    def test_create_rejects_bad_entry(self) -> None:
        with pytest.raises(SystemExit):
            _run("create", "bad", "--set", "review=maybe")
        assert not LoopPreset.objects.filter(name="bad").exists()

    def test_create_accepts_every_canonical_pin(self) -> None:
        # LP-5: --pin validates against the SAME canonical PIN_MODES the model uses.
        for index, mode in enumerate(sorted(PIN_MODES)):
            _run("create", f"pinned-{index}", "--pin", mode)
            assert LoopPreset.objects.get(name=f"pinned-{index}").availability_mode == mode

    def test_create_refuses_a_non_canonical_pin(self) -> None:
        with pytest.raises(SystemExit):
            _run("create", "bad-pin", "--pin", "sideways")
        assert not LoopPreset.objects.filter(name="bad-pin").exists()

    def test_edit_inherit_removes_an_entry(self) -> None:
        LoopPreset.objects.create(name="p", entries={"review": False, "dispatch": True})
        _run("edit", "p", "--set", "review=inherit")
        assert LoopPreset.objects.get(name="p").entries == {"dispatch": True}

    def test_use_activates_an_override(self) -> None:
        LoopPreset.objects.create(name="engaged", entries={"review": True})
        _run("use", "engaged", "--hold")
        override = LoopPresetOverride.objects.current()
        assert override is not None
        assert override.preset_name == "engaged"
        assert override.until is None

    def test_use_with_for_ttl_sets_until(self) -> None:
        LoopPreset.objects.create(name="engaged", entries={})
        _run("use", "engaged", "--for", "2h")
        assert LoopPresetOverride.objects.current().until is not None

    def test_use_with_until_iso_instant_still_works(self) -> None:
        # --until remains a valid spelling of the unified expiry input.
        LoopPreset.objects.create(name="engaged", entries={})
        _run("use", "engaged", "--until", "2099-01-01T00:00:00+00:00")
        until = LoopPresetOverride.objects.current().until
        assert until is not None
        assert until.year == 2099

    def test_use_records_reason_and_show_surfaces_it(self) -> None:
        # LP-6: --reason is stored on the override and rendered on the active WHY line.
        LoopPreset.objects.create(name="engaged", entries={})
        _run("use", "engaged", "--hold", "--reason", "release freeze")
        assert LoopPresetOverride.objects.current().reason == "release freeze"
        payload = json.loads(_run("show", json_output=True))
        assert "release freeze" in payload["active"]["reason"]

    def test_use_without_reason_leaves_it_blank(self) -> None:
        LoopPreset.objects.create(name="engaged", entries={})
        _run("use", "engaged", "--hold")
        assert LoopPresetOverride.objects.current().reason == ""

    def test_use_refuses_unknown_preset(self) -> None:
        with pytest.raises(SystemExit):
            _run("use", "ghost")

    def test_auto_clears_the_override(self) -> None:
        LoopPreset.objects.create(name="engaged", entries={})
        _run("use", "engaged", "--hold")
        _run("auto")
        assert LoopPresetOverride.objects.current() is None

    def test_show_active_reports_why_and_verdicts(self) -> None:
        self._loop("lp-review")
        self._loop("lp-dispatch", enabled=False)
        LoopPreset.objects.create(name="engaged", entries={"lp-review": False, "lp-dispatch": True})
        _run("use", "engaged", "--hold")
        payload = json.loads(_run("show", json_output=True))
        assert payload["active"]["name"] == "engaged"
        assert payload["active"]["layer"] == "override"
        verdicts = {row["name"]: row for row in payload["loops"]}
        assert verdicts["lp-review"]["admitted"] is False
        assert verdicts["lp-review"]["layer"] == "override"
        assert verdicts["lp-dispatch"]["admitted"] is True
        assert verdicts["lp-dispatch"]["layer"] == "override"

    def test_show_active_none_when_no_preset(self) -> None:
        self._loop("lp-base")
        payload = json.loads(_run("show", json_output=True))
        assert payload["active"] is None
        assert {row["name"]: row for row in payload["loops"]}["lp-base"]["layer"] == "base"

    def test_show_named_warns_on_unknown_loop(self) -> None:
        LoopPreset.objects.create(name="p", entries={"nonexistent_loop": False})
        out = _run("show", "p")
        assert "nonexistent_loop" in out
