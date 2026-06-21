"""``manage.py loop_state`` — pause/resume/disable/enable a mini-loop (#1913).

Integration-first against the real DB via ``call_command``: each subcommand
performs the atomic ``LoopState`` transition and the transition is idempotent
(re-issuing it is a no-op that leaves the one row in the target status). The
command re-reads and reports the landed status so the operator sees the verified
state, not just an echo of the request.
"""

import json
from io import StringIO

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
        out = _run("status", "never-touched", "--json")
        payload = json.loads(out)
        assert payload["name"] == "never-touched"
        assert payload["status"] == "enabled"


class TestLoopStateSetsLoopRowEnabled(TestCase):
    """``enable``/``disable`` must set ``Loop.enabled`` — the master-tick source of truth.

    The #2584 unified verdict gates a loop on BOTH ``Loop.enabled`` AND the
    ``LoopState`` control plane. Writing only the ``LoopState`` kill-switch left
    ``Loop.enabled`` stale, so ``t3 loop enable <name>`` reported success while
    the master tick's ``not row.enabled`` gate kept skipping the loop. These pin
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

    def test_enable_disable_are_no_ops_for_a_name_with_no_loop_row(self) -> None:
        # A control-plane verb on an unknown loop name still writes LoopState
        # (the existing absent-row → ENABLED contract) and does not crash.
        _run("disable", "no-such-loop")
        assert LoopState.objects.status_of("no-such-loop") is LoopStatus.DISABLED
        assert not Loop.objects.filter(name="no-such-loop").exists()
