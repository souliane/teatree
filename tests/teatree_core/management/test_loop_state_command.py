"""``manage.py loop_state`` — pause/resume/disable/enable a mini-loop (#1913).

Integration-first against the real DB via ``call_command``: each subcommand
performs the atomic ``LoopState`` transition and the transition is idempotent
(re-issuing it is a no-op that leaves the one row in the target status). The
command re-reads and reports the landed status so the operator sees the verified
state, not just an echo of the request.
"""

import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import Loop, LoopState, LoopStatus, Prompt


def _run(*args: str) -> str:
    out = StringIO()
    call_command("loop_state", *args, stdout=out)
    return out.getvalue()


def _loop(name: str, *, enabled: bool) -> Loop:
    prompt, _ = Prompt.objects.get_or_create(name=f"{name}-prompt", defaults={"body": "do x"})
    return Loop.objects.update_or_create(
        name=name,
        defaults={"delay_seconds": 60, "prompt": prompt, "script": "", "enabled": enabled},
    )[0]


class TestLoopStateCommand(TestCase):
    def setUp(self) -> None:
        # Real loop rows: every verb now validates the name against the Loop table (#3117).
        _loop("review", enabled=True)
        _loop("ship", enabled=True)
        _loop("tickets", enabled=True)

    def test_pause_sets_paused_status(self) -> None:
        _run("pause", "review")
        assert LoopState.objects.status_of("review") is LoopStatus.PAUSED

    def test_disable_sets_disabled_status(self) -> None:
        _run("disable", "ship")
        assert LoopState.objects.status_of("ship") is LoopStatus.DISABLED

    def test_resume_returns_to_enabled(self) -> None:
        _run("pause", "review")
        _run("resume", "review")
        assert LoopState.objects.status_of("review") is LoopStatus.ENABLED

    def test_enable_returns_to_enabled_from_disabled(self) -> None:
        _run("disable", "ship")
        _run("enable", "ship")
        assert LoopState.objects.status_of("ship") is LoopStatus.ENABLED

    def test_pause_is_idempotent(self) -> None:
        _run("pause", "review")
        _run("pause", "review")
        assert LoopState.objects.filter(name="review", status=LoopStatus.PAUSED).count() == 1

    def test_output_reports_the_landed_status(self) -> None:
        out = _run("pause", "review")
        assert "review" in out
        assert "paused" in out.lower()

    def test_json_output_carries_name_and_status(self) -> None:
        out = _run("disable", "ship", "--json")
        payload = json.loads(out)
        assert payload["name"] == "ship"
        assert payload["status"] == "disabled"

    def test_status_subcommand_reports_enabled_for_untouched_loop(self) -> None:
        # A known loop with no LoopState row resolves to the ENABLED default.
        out = _run("status", "tickets", "--json")
        payload = json.loads(out)
        assert payload["name"] == "tickets"
        assert payload["status"] == "enabled"


class TestStatusSubcommandIsAReadNotAMutation(TestCase):
    """``status`` is a strict READ — no mutation, and its output reads like one.

    The shared ``_report`` printed ``OK    loop 'x' is now <status>.`` — the
    mutation-verb phrasing — so a ``status`` read was indistinguishable from a
    pause/enable that had just changed the loop. The read now prints a
    status-shaped line, and never writes a ``LoopState`` row.
    """

    def setUp(self) -> None:
        _loop("review", enabled=True)

    def test_status_leaves_an_enabled_loop_enabled_and_writes_no_row(self) -> None:
        _run("status", "review")
        assert LoopState.objects.status_of("review") is LoopStatus.ENABLED
        assert not LoopState.objects.filter(name="review").exists()

    def test_status_text_reads_as_a_status_not_a_mutation(self) -> None:
        out = _run("status", "review")
        assert "is now" not in out
        assert "status:" in out.lower()
        assert "ENABLED" in out

    def test_status_reports_a_paused_loop_without_changing_it(self) -> None:
        _run("pause", "review")
        out = _run("status", "review")
        assert "PAUSED" in out
        assert LoopState.objects.status_of("review") is LoopStatus.PAUSED


class TestLoopStateSetsLoopRowEnabled(TestCase):
    """``enable``/``disable`` must set ``Loop.enabled`` — the master-tick source of truth.

    The #2584 unified verdict gates a loop on BOTH ``Loop.enabled`` AND the
    ``LoopState`` control plane. Writing only the ``LoopState`` kill-switch left
    ``Loop.enabled`` stale, so ``t3 loop enable <name>`` reported success while
    the loop tick's ``not row.enabled`` gate kept skipping the loop. These pin
    both columns moving together.
    """

    def test_enable_sets_loop_row_enabled_true(self) -> None:
        _loop("dispatch", enabled=False)
        _run("enable", "dispatch")
        assert Loop.objects.get(name="dispatch").enabled is True

    def test_disable_sets_loop_row_enabled_false(self) -> None:
        _loop("ship", enabled=True)
        _run("disable", "ship")
        assert Loop.objects.get(name="ship").enabled is False

    def test_enable_also_clears_the_loop_state_hold(self) -> None:
        _loop("tickets", enabled=False)
        _run("disable", "tickets")
        _run("enable", "tickets")
        # Both planes agree the loop runs again.
        assert Loop.objects.get(name="tickets").enabled is True
        assert LoopState.objects.status_of("tickets") is LoopStatus.ENABLED

    def test_disable_also_sets_the_loop_state_kill_switch(self) -> None:
        _loop("housekeeping", enabled=True)
        _run("disable", "housekeeping")
        # Both planes agree the loop is held.
        assert Loop.objects.get(name="housekeeping").enabled is False
        assert LoopState.objects.status_of("housekeeping") is LoopStatus.DISABLED

    def test_resume_returns_loop_row_to_enabled(self) -> None:
        _loop("audit", enabled=False)
        _run("resume", "audit")
        assert Loop.objects.get(name="audit").enabled is True


class TestUnknownLoopNameRefused(TestCase):
    """#3117: every verb refuses a name with no matching ``Loop`` row before touching ``LoopState``.

    ``pause``/``resume``/``disable``/``enable``/``status`` on an unknown name used
    to write (or, for ``status``, silently resolve to) a ``LoopState`` row for a
    loop that does not exist — so a typo in a pause command reported success and
    paused nothing. Each verb now exits non-zero, names the unknown loop, points
    at ``t3 loops list``, and writes NO ``LoopState`` row.
    """

    _BOGUS = "totally_bogus_loop_xyz"

    def _refuse(self, *args: str) -> str:
        out = StringIO()
        with pytest.raises(SystemExit) as caught:
            call_command("loop_state", *args, self._BOGUS, stdout=out)
        assert caught.value.code == 2
        return out.getvalue()

    def test_pause_unknown_name_refused_no_row(self) -> None:
        out = self._refuse("pause")
        assert self._BOGUS in out
        assert "t3 loops list" in out
        assert not LoopState.objects.filter(name=self._BOGUS).exists()

    def test_resume_unknown_name_refused_no_row(self) -> None:
        self._refuse("resume")
        assert not LoopState.objects.filter(name=self._BOGUS).exists()

    def test_disable_unknown_name_refused_no_row(self) -> None:
        self._refuse("disable")
        assert not LoopState.objects.filter(name=self._BOGUS).exists()

    def test_enable_unknown_name_refused_no_row(self) -> None:
        self._refuse("enable")
        assert not LoopState.objects.filter(name=self._BOGUS).exists()

    def test_status_unknown_name_refused_never_prints_enabled(self) -> None:
        out = self._refuse("status")
        assert "ENABLED" not in out.upper()
        assert not LoopState.objects.filter(name=self._BOGUS).exists()

    def test_known_loop_still_pauses(self) -> None:
        # No-regression: a real loop still pauses.
        _loop("dispatch", enabled=True)
        _run("pause", "dispatch")
        assert LoopState.objects.status_of("dispatch") is LoopStatus.PAUSED
