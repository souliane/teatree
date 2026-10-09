"""``manage.py loop_preset`` — create/edit/use/auto/show/list against a real DB.

Integration-first: drives the management command via ``call_command`` and asserts
the DB effect (the override row, the preset row) plus the rendered WHY output.
"""

import io
import json

import django.test
import pytest
from django.core.management import call_command

from teatree.core.models import Loop, Mode, ModeOverride


def _run(*args: str, **kwargs: object) -> str:
    # emit() sends JSON to stdout and the human view to stderr — one channel per
    # call — so their concatenation is the single populated output stream.
    out = io.StringIO()
    err = io.StringIO()
    call_command("loop_preset", *args, stdout=out, stderr=err, **kwargs)
    return out.getvalue() + err.getvalue()


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLoopPresetCommand(django.test.TestCase):
    def _loop(self, name: str, *, enabled: bool | None = None) -> Loop:
        return Loop.objects.create(
            name=name, delay_seconds=60, script=f"src/teatree/loops/{name}/loop.py", enabled=enabled
        )

    def test_a_newborn_preset_runs_nothing_and_is_total(self) -> None:
        # B1: every preset holds an opinion on EVERY live loop, and a newborn one says
        # off to all of them — so admitting a loop is always a deliberate edit.
        self._loop("lp-review")
        self._loop("lp-dispatch")
        _run("create", "deep-work", "--description", "deep work")
        preset = Mode.objects.get(name="deep-work")
        assert set(preset.entries) == set(Loop.objects.values_list("name", flat=True))
        assert not any(preset.entries.values())
        assert preset.description == "deep work"
        assert "deep-work" in _run("list")

    def test_edit_admits_a_loop_the_newborn_preset_refused(self) -> None:
        self._loop("lp-review")
        _run("create", "deep-work")
        _run("edit", "deep-work", "--set", "lp-review=on")
        assert Mode.objects.get(name="deep-work").entries["lp-review"] is True

    def test_edit_rejects_a_bad_entry(self) -> None:
        self._loop("lp-review")
        _run("create", "bad")
        with pytest.raises(SystemExit):
            _run("edit", "bad", "--set", "lp-review=maybe")
        assert Mode.objects.get(name="bad").entries["lp-review"] is False

    def test_use_activates_an_override(self) -> None:
        Mode.objects.create(name="present", entries={"review": True})
        _run("use", "present", "--reason", "pinned by the test")
        override = ModeOverride.objects.current()
        assert override is not None
        assert override.preset_name == "present"
        assert override.expected_lift_at is None

    def test_lift_by_is_advisory_and_nothing_expires_the_override(self) -> None:
        # A5/A7: no override auto-clears. `--lift-by` is what the watcher reminds
        # against, and the row stands until someone lifts it deliberately.
        Mode.objects.create(name="present", entries={})
        _run("use", "present", "--reason", "release freeze", "--lift-by", "2h")
        override = ModeOverride.objects.current()
        assert override is not None
        assert override.expected_lift_at is not None

    def test_use_records_reason_and_show_surfaces_it(self) -> None:
        # LP-6: --reason is stored on the override and rendered on the active WHY line.
        Mode.objects.create(name="present", entries={})
        _run("use", "present", "--reason", "release freeze")
        assert ModeOverride.objects.current().reason == "release freeze"
        payload = json.loads(_run("show", json_output=True))
        assert "release freeze" in payload["active"]["reason"]

    def test_use_without_a_reason_is_refused(self) -> None:
        # A8: without a recorded reason nothing can judge whether the posture still
        # applies, so the only possible policy would be a timer — which A6 got wrong.
        Mode.objects.create(name="present", entries={})
        with pytest.raises(SystemExit):
            _run("use", "present")
        assert ModeOverride.objects.current() is None

    def test_use_refuses_unknown_preset(self) -> None:
        with pytest.raises(SystemExit):
            _run("use", "ghost")

    def test_auto_clears_the_override(self) -> None:
        Mode.objects.create(name="present", entries={})
        _run("use", "present", "--reason", "pinned by the test")
        _run("auto")
        assert ModeOverride.objects.current() is None

    def test_show_active_reports_why_and_verdicts(self) -> None:
        # No manual override on either row, so the PRESET is the deciding layer and its
        # entry is the whole answer.
        self._loop("lp-review", enabled=None)
        self._loop("lp-dispatch", enabled=None)
        Mode.objects.create(name="present", entries={"lp-review": False, "lp-dispatch": True})
        _run("use", "present", "--reason", "pinned by the test")
        payload = json.loads(_run("show", json_output=True))
        assert payload["active"]["name"] == "present"
        assert payload["active"]["layer"] == "override"
        verdicts = {row["name"]: row for row in payload["loops"]}
        assert verdicts["lp-review"]["admitted"] is False
        assert verdicts["lp-review"]["layer"] == "override"
        assert verdicts["lp-dispatch"]["admitted"] is True
        assert verdicts["lp-dispatch"]["layer"] == "override"

    def test_show_active_none_when_no_preset(self) -> None:
        self._loop("lp-base", enabled=True)
        payload = json.loads(_run("show", json_output=True))
        assert payload["active"] is None
        assert {row["name"]: row for row in payload["loops"]}["lp-base"]["layer"] == "manual"

    def test_show_named_warns_on_unknown_loop(self) -> None:
        Mode.objects.create(name="p", entries={"nonexistent_loop": False})
        out = _run("show", "p")
        assert "nonexistent_loop" in out
