"""``manage.py loops_toggle`` — enable/disable a single DB ``Loop`` row.

Integration-first against the real DB via ``call_command``: each subcommand
flips ``Loop.enabled`` for the named row, is idempotent, and reports the landed
state. An unknown loop name is a hard error (non-zero exit) that names the valid
loops. This is the per-instance fleet seam (``Loop.enabled``); it deliberately
leaves the ``LoopState`` control plane untouched, distinct from the singular
``t3 loop enable``/``disable``.
"""

import json
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Loop, LoopState, Prompt


def _run(*args: str) -> str:
    out = StringIO()
    call_command("loops_toggle", *args, stdout=out)
    return out.getvalue()


def _loop(name: str, *, enabled: bool) -> Loop:
    prompt, _ = Prompt.objects.get_or_create(name=f"{name}-prompt", defaults={"body": "do x"})
    return Loop.objects.update_or_create(
        name=name,
        defaults={"delay_seconds": 60, "prompt": prompt, "script": "", "enabled": enabled},
    )[0]


class TestLoopsToggleFlipsEnabled(TestCase):
    def test_enable_sets_loop_enabled_true(self) -> None:
        _loop("idle-stack-reaper", enabled=False)
        _run("enable", "idle-stack-reaper")
        assert Loop.objects.get(name="idle-stack-reaper").enabled is True

    def test_disable_sets_loop_enabled_false(self) -> None:
        _loop("tickets", enabled=True)
        _run("disable", "tickets")
        assert Loop.objects.get(name="tickets").enabled is False

    def test_disabled_loop_is_excluded_from_enabled_queryset(self) -> None:
        _loop("resource-pressure", enabled=True)
        _run("disable", "resource-pressure")
        assert "resource-pressure" not in set(Loop.objects.enabled().values_list("name", flat=True))

    def test_enabled_loop_is_included_in_enabled_queryset(self) -> None:
        _loop("housekeeping", enabled=False)
        _run("enable", "housekeeping")
        assert "housekeeping" in set(Loop.objects.enabled().values_list("name", flat=True))


class TestLoopsToggleIsIdempotent(TestCase):
    def test_disabling_an_already_disabled_loop_is_a_no_op_success(self) -> None:
        _loop("pane-reaper", enabled=False)
        out = _run("disable", "pane-reaper")
        assert Loop.objects.get(name="pane-reaper").enabled is False
        assert "pane-reaper: disabled" in out

    def test_enabling_an_already_enabled_loop_is_a_no_op_success(self) -> None:
        _loop("tickets", enabled=True)
        out = _run("enable", "tickets")
        assert Loop.objects.get(name="tickets").enabled is True
        assert "tickets: enabled" in out


class TestLoopsToggleReportsState(TestCase):
    def test_enable_prints_scriptable_enabled_line(self) -> None:
        _loop("review", enabled=False)
        assert _run("enable", "review").strip() == "review: enabled"

    def test_disable_prints_scriptable_disabled_line(self) -> None:
        _loop("review", enabled=True)
        assert _run("disable", "review").strip() == "review: disabled"

    def test_json_output_carries_name_and_enabled(self) -> None:
        _loop("inbox", enabled=True)
        payload = json.loads(_run("disable", "inbox", "--json"))
        assert payload == {"name": "inbox", "enabled": False}


class TestLoopsToggleUnknownName(TestCase):
    def test_unknown_name_exits_non_zero_and_names_valid_loops(self) -> None:
        _loop("tickets", enabled=True)
        err = StringIO()
        with pytest.raises(SystemExit) as exc_info:
            call_command("loops_toggle", "disable", "no-such-loop", stderr=err)
        assert exc_info.value.code == 2
        message = err.getvalue()
        assert "no-such-loop" in message
        assert "tickets" in message

    def test_unknown_name_creates_no_phantom_row(self) -> None:
        with pytest.raises(SystemExit):
            call_command("loops_toggle", "enable", "no-such-loop", stderr=StringIO())
        assert not Loop.objects.filter(name="no-such-loop").exists()


class TestLoopsToggleLeavesLoopStateUntouched(TestCase):
    """The plural ``loops`` toggle is the ``Loop.enabled`` fleet seam only.

    Unlike the singular ``t3 loop disable`` (which also writes the ``LoopState``
    control plane), this command flips only the row column, so an operator using
    it for per-instance fleet scoping never touches durable operator intent.
    """

    def test_disable_writes_no_loop_state_row(self) -> None:
        _loop("tickets", enabled=True)
        _run("disable", "tickets")
        assert not LoopState.objects.filter(name="tickets").exists()


class TestLoopsToggleTimerReconcileIsBestEffort(TestCase):
    def test_toggle_still_lands_when_timer_reconcile_raises(self) -> None:
        _loop("tickets", enabled=True)
        with patch("teatree.loops.timer_reconciler.ensure_loop_timers", side_effect=RuntimeError("boom")):
            out = _run("disable", "tickets")
        assert Loop.objects.get(name="tickets").enabled is False
        assert "tickets: disabled" in out
