"""``manage.py loops_list`` — list DB-configured autonomous loops (#1796).

Integration-first: drives the real ``loops_list`` command via ``call_command``
against the seeded :class:`Loop` table, asserting the rendered text columns
(interval + daily cadence) and the ``--json`` shape. Read-only — never mutates.
"""

import datetime as dt
import io
import json

import django.test
from django.core.management import call_command
from django.utils import timezone

from teatree.core.models import Loop, LoopState, Prompt


def _prompt(name: str = "demo-prompt") -> Prompt:
    """A reusable :class:`Prompt` FK target for loops under test (#2513)."""
    prompt, _ = Prompt.objects.get_or_create(name=name, defaults={"body": "do x"})
    return prompt


def _run(*args: str) -> str:
    out = io.StringIO()
    call_command("loops_list", *args, stdout=out)
    return out.getvalue()


@django.test.override_settings(USE_TZ=True)
class TestLoopsListText(django.test.TestCase):
    def test_lists_seeded_interval_loop_with_cadence(self) -> None:
        # An opt-in loop (``ship`` is externally-visible ⇒ seeded paused) renders
        # ``disabled`` with its cadence column. The sound operational core (e.g.
        # ``tickets``) now seeds ENABLED, so it is not the disabled-render case.
        line = next(ln for ln in _run().splitlines() if ln.strip().startswith("ship"))
        assert "disabled" in line
        assert "every 300s" in line

    def test_lists_seeded_daily_loop_shows_schedule(self) -> None:
        line = next(ln for ln in _run().splitlines() if ln.strip().startswith("news"))
        assert "daily 08:00" in line

    def test_disabled_loop_marked_disabled(self) -> None:
        Loop.objects.create(name="demo-off", delay_seconds=60, prompt=_prompt(), enabled=False)
        line = next(ln for ln in _run().splitlines() if ln.strip().startswith("demo-off"))
        assert "disabled" in line

    def test_never_run_interval_loop_renders_due(self) -> None:
        Loop.objects.create(name="demo-new", delay_seconds=60, prompt=_prompt())
        line = next(ln for ln in _run().splitlines() if ln.strip().startswith("demo-new"))
        assert "last —" in line
        assert "next due" in line

    def test_colleague_facing_loop_is_tagged(self) -> None:
        Loop.objects.create(name="demo-cf", delay_seconds=60, prompt=_prompt(), colleague_facing=True)
        line = next(ln for ln in _run().splitlines() if ln.strip().startswith("demo-cf"))
        assert "colleague-facing" in line

    def test_non_colleague_facing_loop_is_not_tagged(self) -> None:
        Loop.objects.create(name="demo-internal", delay_seconds=60, prompt=_prompt(), colleague_facing=False)
        line = next(ln for ln in _run().splitlines() if ln.strip().startswith("demo-internal"))
        assert "colleague-facing" not in line


@django.test.override_settings(USE_TZ=True)
class TestLoopsListDescription(django.test.TestCase):
    def test_renders_description_on_its_own_line(self) -> None:
        Loop.objects.create(
            name="demo-desc",
            delay_seconds=60,
            prompt=_prompt(),
            description="Does the thing every minute.",
        )
        lines = _run().splitlines()
        status = next(ln for ln in lines if ln.strip().startswith("demo-desc"))
        # The status columns stay on their own line (no description bleed-in)…
        assert "Does the thing every minute." not in status
        # …and the description renders on the indented continuation line below it.
        desc_line = lines[lines.index(status) + 1]
        assert desc_line.strip() == "Does the thing every minute."

    def test_blank_description_emits_no_continuation_line(self) -> None:
        Loop.objects.create(name="demo-nodesc", delay_seconds=60, prompt=_prompt(), description="")
        lines = _run().splitlines()
        status = next(ln for ln in lines if ln.strip().startswith("demo-nodesc"))
        following = lines[lines.index(status) + 1 :]
        # The next non-empty line is another loop's status row, not a blank
        # description continuation line.
        assert not following or following[0].strip()


@django.test.override_settings(USE_TZ=True)
class TestLoopsListJson(django.test.TestCase):
    def test_json_carries_description(self) -> None:
        Loop.objects.create(
            name="demo-json-desc", delay_seconds=60, prompt=_prompt(), description="A useful one-liner."
        )
        payload = json.loads(_run("--json"))
        demo = next(e for e in payload["loops"] if e["name"] == "demo-json-desc")
        assert demo["description"] == "A useful one-liner."

    def test_json_interval_loop_shape(self) -> None:
        Loop.objects.create(
            name="demo-json", delay_seconds=120, prompt=_prompt(), last_run_at=timezone.now() - dt.timedelta(seconds=30)
        )
        payload = json.loads(_run("--json"))
        demo = next(e for e in payload["loops"] if e["name"] == "demo-json")
        assert demo["enabled"] is True
        assert demo["delay_seconds"] == 120
        assert demo["daily_at"] == ""
        assert demo["last_run_at"] != ""
        assert demo["due"] is False

    def test_json_daily_loop_carries_schedule(self) -> None:
        payload = json.loads(_run("--json"))
        news = next(e for e in payload["loops"] if e["name"] == "news")
        assert news["daily_at"] == "08:00"
        assert news["cadence"] == "daily 08:00"

    def test_json_carries_colleague_facing(self) -> None:
        Loop.objects.create(name="demo-json-cf", delay_seconds=60, prompt=_prompt(), colleague_facing=True)
        payload = json.loads(_run("--json"))
        demo = next(e for e in payload["loops"] if e["name"] == "demo-json-cf")
        assert demo["colleague_facing"] is True


@django.test.override_settings(USE_TZ=True)
class TestLoopsListReflectsPauseHold(django.test.TestCase):
    """#3117 bonus: a ``LoopState`` pause/disable hold is visible in the state column.

    ``t3 loop pause`` holds a loop via ``LoopState`` WITHOUT flipping
    ``Loop.enabled``, so the row alone still read ``enabled`` — a pause was
    invisible in ``t3 loops list``. The state column now folds in the hold so a
    pause is confirmable at a glance.
    """

    def test_paused_loop_renders_paused_not_enabled(self) -> None:
        Loop.objects.create(name="demo-held", delay_seconds=60, prompt=_prompt(), enabled=True)
        LoopState.objects.pause("demo-held")
        line = next(ln for ln in _run().splitlines() if ln.strip().startswith("demo-held"))
        assert "paused" in line
        assert "enabled" not in line
        assert "next —" in line

    def test_state_disabled_hold_renders_disabled(self) -> None:
        Loop.objects.create(name="demo-killed", delay_seconds=60, prompt=_prompt(), enabled=True)
        LoopState.objects.disable("demo-killed")
        line = next(ln for ln in _run().splitlines() if ln.strip().startswith("demo-killed"))
        assert "disabled" in line

    def test_json_carries_effective_status(self) -> None:
        Loop.objects.create(name="demo-held-json", delay_seconds=60, prompt=_prompt(), enabled=True)
        LoopState.objects.pause("demo-held-json")
        demo = next(e for e in json.loads(_run("--json"))["loops"] if e["name"] == "demo-held-json")
        assert demo["status"] == "paused"
        # ``enabled`` keeps its row-flag meaning for backward compatibility.
        assert demo["enabled"] is True


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestLoopsListPresetEffectiveColumn(django.test.TestCase):
    """#3159: a preset-masked loop reads ``masked (…)`` instead of silently vanishing."""

    def _activate(self, preset_name: str, entries: dict[str, bool]) -> None:
        from teatree.core.models import (  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
            LoopPreset,
            LoopPresetOverride,
        )

        LoopPreset.objects.create(name=preset_name, entries=entries)
        LoopPresetOverride.objects.set_override(preset_name)

    def test_masked_off_loop_is_annotated(self) -> None:
        Loop.objects.create(name="demo-mask", delay_seconds=60, prompt=_prompt(), enabled=True)
        self._activate("heads-down", {"demo-mask": False})
        line = next(ln for ln in _run().splitlines() if ln.strip().startswith("demo-mask"))
        assert "masked" in line

    def test_forced_on_loop_is_annotated(self) -> None:
        Loop.objects.create(name="demo-forced", delay_seconds=60, prompt=_prompt(), enabled=False)
        self._activate("engaged", {"demo-forced": True})
        line = next(ln for ln in _run().splitlines() if ln.strip().startswith("demo-forced"))
        assert "forced-on" in line

    def test_json_carries_effective_layer(self) -> None:
        Loop.objects.create(name="demo-json-mask", delay_seconds=60, prompt=_prompt(), enabled=True)
        self._activate("heads-down", {"demo-json-mask": False})
        demo = next(e for e in json.loads(_run("--json"))["loops"] if e["name"] == "demo-json-mask")
        assert demo["effective_layer"] == "override"
        assert demo["effective_admitted"] is False

    def test_no_preset_leaves_base_layer(self) -> None:
        Loop.objects.create(name="demo-base", delay_seconds=60, prompt=_prompt(), enabled=True)
        demo = next(e for e in json.loads(_run("--json"))["loops"] if e["name"] == "demo-base")
        assert demo["effective_layer"] == "base"


@django.test.override_settings(USE_TZ=True)
class TestLoopsListReadOnly(django.test.TestCase):
    def test_no_rows_created_or_mutated(self) -> None:
        before = Loop.objects.count()
        _run()
        _run("--json")
        assert Loop.objects.count() == before
