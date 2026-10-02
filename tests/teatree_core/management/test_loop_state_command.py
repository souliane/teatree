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
from django.test import TestCase, override_settings
from django.utils import timezone
from django_tasks_db.models import DBTaskResult

from teatree.core.models import Loop, LoopState, LoopStatus, Prompt
from teatree.loops import timer_chains


def _run(*args: str) -> str:
    # emit() sends JSON to stdout and the human view to stderr — one channel per
    # call — so their concatenation is the single populated output stream.
    out = StringIO()
    err = StringIO()
    call_command("loop_state", *args, stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


def _loop(name: str, *, enabled: bool | None = None) -> Loop:
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


class TestTheHoldVerbsMoveTheHoldPlaneAlone(TestCase):
    """``pause``/``resume``/``disable``/``enable`` are HOLD-plane verbs, and only that.

    ``Loop.enabled`` is the MANUAL override slot now, written solely through
    ``set_manual_override`` (which requires a reason). A hold verb that also wrote it
    would be a second, reasonless writer of the layer above it — so these pin that the
    hold moves and the manual layer is left exactly as the operator left it.
    """

    def test_disable_holds_the_loop_without_touching_the_manual_layer(self) -> None:
        _loop("ship")
        _run("disable", "ship")
        assert LoopState.objects.status_of("ship") is LoopStatus.DISABLED
        assert Loop.objects.get(name="ship").enabled is None

    def test_enable_clears_the_hold_without_touching_the_manual_layer(self) -> None:
        _loop("tickets")
        _run("disable", "tickets")
        _run("enable", "tickets")
        assert LoopState.objects.status_of("tickets") is LoopStatus.ENABLED
        assert Loop.objects.get(name="tickets").enabled is None

    def test_resume_clears_a_pause_without_touching_the_manual_layer(self) -> None:
        _loop("audit")
        _run("pause", "audit")
        _run("resume", "audit")
        assert LoopState.objects.status_of("audit") is LoopStatus.ENABLED
        assert Loop.objects.get(name="audit").enabled is None

    def test_a_manual_override_survives_a_hold_and_its_release(self) -> None:
        # The property the split exists for: the human's recorded decision is not
        # collateral damage of an emergency hold.
        _loop("housekeeping")
        Loop.objects.set_manual_override("housekeeping", runs=False, reason="pinned by the test")
        _run("disable", "housekeeping")
        _run("enable", "housekeeping")
        assert Loop.objects.get(name="housekeeping").enabled is False


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
        err = StringIO()
        with pytest.raises(SystemExit) as caught:
            call_command("loop_state", *args, self._BOGUS, stdout=out, stderr=err)
        assert caught.value.code == 2
        return out.getvalue() + err.getvalue()

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


class TestOverrideCommand(TestCase):
    """``loop_state override`` — the MANUAL layer, and the sole writer of it (A3).

    Sets the tri-state manual value (on/off/clear) on ``Loop.enabled``, which beats the
    preset. Setting one REQUIRES a reason: without it nothing can judge whether the
    override still applies, so the only possible policy would be a timer — the thing A6
    got wrong. ``--lift-by`` is advisory and nothing enforces it (A5/A7).
    """

    def setUp(self) -> None:
        _loop("review")
        _loop("news")

    def test_override_on_forces_the_loop_to_run(self) -> None:
        _run("override", "review", "on", "--reason", "incident firefight")
        assert Loop.objects.get(name="review").enabled is True

    def test_override_off_forces_the_loop_to_stop(self) -> None:
        _run("override", "news", "off", "--reason", "incident firefight")
        assert Loop.objects.get(name="news").enabled is False

    def test_override_clear_hands_the_loop_back_to_the_preset(self) -> None:
        _run("override", "review", "on", "--reason", "incident firefight")
        _run("override", "review", "clear")
        assert Loop.objects.get(name="review").enabled is None

    def test_an_override_without_a_reason_is_refused(self) -> None:
        with pytest.raises(SystemExit) as caught:
            _run("override", "review", "on")
        assert caught.value.code == 2
        assert Loop.objects.get(name="review").enabled is None

    def test_lift_by_is_recorded_and_advisory(self) -> None:
        _run("override", "review", "on", "--reason", "incident firefight", "--lift-by", "2h")
        row = Loop.objects.get(name="review")
        assert row.override_expected_lift_at is not None
        assert row.override_expected_lift_at > timezone.now()

    def test_override_records_reason(self) -> None:
        _run("override", "review", "on", "--reason", "incident firefight")
        assert Loop.objects.get(name="review").override_reason == "incident firefight"

    def test_override_unknown_name_refused(self) -> None:
        out = StringIO()
        with pytest.raises(SystemExit) as caught:
            call_command("loop_state", "override", "no_such_loop", "on", stdout=out)
        assert caught.value.code == 2
        assert not LoopState.objects.filter(name="no_such_loop").exists()

    def test_override_invalid_state_refused(self) -> None:
        out = StringIO()
        with pytest.raises(SystemExit) as caught:
            call_command("loop_state", "override", "review", "maybe", stdout=out)
        assert caught.value.code == 2


@override_settings(
    TASKS={"default": {"BACKEND": "django_tasks_db.DatabaseBackend", "QUEUES": ["default", "loops", "cheap"]}}
)
class TestOverrideReconcilesTheTimerChain(TestCase):
    """The manual layer outranks the preset, so writing it changes chain membership now.

    ``resume``/``disable``/``enable`` all reconcile at their chokepoint; ``override`` did
    not, so a force-ON left a loop admitted with nothing driving it — and a force-OFF left
    a timer firing into a refusal — until the ~5-minute reconcile chain caught up (#4196).
    """

    def setUp(self) -> None:
        Loop.objects.all().delete()
        DBTaskResult.objects.all().delete()
        _loop("inbox", enabled=False)
        Loop.objects.filter(name="inbox").update(script="src/teatree/loops/inbox/loop.py", prompt=None)

    def test_force_on_heads_the_chain_at_once(self) -> None:
        _run("override", "inbox", "on", "--reason", "pinned by the test")
        assert len(timer_chains.pending_loop_timers("inbox")) == 1

    def test_force_off_prunes_the_chain_at_once(self) -> None:
        Loop.objects.filter(name="inbox").update(enabled=True)
        timer_chains.enqueue_loop_timer("inbox", run_after=timezone.now())
        _run("override", "inbox", "off", "--reason", "pinned by the test")
        assert timer_chains.pending_loop_timers("inbox") == []
