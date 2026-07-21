"""teatree.loop.loop_state_db — the single combined enable verdict over the DB.

``loop_state_admits`` is the ONE pure predicate every enable-decision site
resolves through: the standalone ``loop_enabled`` single-lookup (the off-live-tick
loop gates) and the live loop-table tick both apply it, so the verdict can never
drift into a tier-subset. ``loop_held_in_db`` is the durable per-loop
PAUSE/DISABLE read; it fails SAFE to no-hold on a read error but must WARN (not
whisper at debug) so a silently-unheld loop is observable (#2584 / holistic 3c#5).
"""

from unittest.mock import patch

import django.test

from teatree.core.models import Loop, LoopState, Prompt
from teatree.loop.loop_state_db import loop_enabled, loop_held_in_db, loop_state_admits


class TestLoopStateAdmits(django.test.SimpleTestCase):
    """The pure combined verdict: configured-enabled AND not runtime-held."""

    def test_configured_and_unheld_admits(self) -> None:
        assert loop_state_admits(configured_enabled=True, held=False, preset_state=None, forced=None) is True

    def test_held_is_not_admitted_even_when_configured(self) -> None:
        assert loop_state_admits(configured_enabled=True, held=True, preset_state=None, forced=None) is False

    def test_not_configured_is_not_admitted_even_when_unheld(self) -> None:
        assert loop_state_admits(configured_enabled=False, held=False, preset_state=None, forced=None) is False

    def test_not_configured_and_held_is_not_admitted(self) -> None:
        assert loop_state_admits(configured_enabled=False, held=True, preset_state=None, forced=None) is False

    def test_none_preset_is_byte_for_byte_the_two_plane_verdict(self) -> None:
        # The #3159 empty-table no-op: an explicit `preset_state=None` (what the
        # resolver returns with no preset) resolves exactly as the pre-#3159
        # `configured_enabled and not held`. There is no neutral default —
        # preset_state is required at every call site (the LP-3 structural guard).
        for configured in (True, False):
            for held in (True, False):
                assert loop_state_admits(configured_enabled=configured, held=held, preset_state=None, forced=None) == (
                    configured and not held
                )

    def test_preset_force_on_overrides_disabled_base(self) -> None:
        assert loop_state_admits(configured_enabled=False, held=False, preset_state=True, forced=None) is True

    def test_preset_force_off_overrides_enabled_base(self) -> None:
        assert loop_state_admits(configured_enabled=True, held=False, preset_state=False, forced=None) is False

    def test_hold_still_wins_over_a_force_on_preset(self) -> None:
        assert loop_state_admits(configured_enabled=True, held=True, preset_state=True, forced=None) is False


@django.test.override_settings(USE_TZ=True)
class TestLoopEnabledCombinedVerdict(django.test.TestCase):
    """``loop_enabled(name)`` is ``Loop.enabled`` AND not ``LoopState``-held — one verdict."""

    def _loop(self, name: str, *, enabled: bool = True) -> Loop:
        prompt, _ = Prompt.objects.get_or_create(name=f"{name}-p", defaults={"body": "x"})
        return Loop.objects.update_or_create(
            name=name, defaults={"delay_seconds": 60, "prompt": prompt, "script": "", "enabled": enabled}
        )[0]

    def test_enabled_and_unheld_is_true(self) -> None:
        self._loop("le-on")
        assert loop_enabled("le-on") is True

    def test_configured_disabled_is_false(self) -> None:
        self._loop("le-off", enabled=False)
        assert loop_enabled("le-off") is False

    def test_loopstate_hold_stops_a_configured_loop(self) -> None:
        self._loop("le-held")
        LoopState.objects.disable("le-held")
        assert loop_enabled("le-held") is False

    def test_missing_row_is_false(self) -> None:
        assert loop_enabled("le-absent") is False

    def test_active_preset_force_off_masks_an_enabled_loop(self) -> None:
        from teatree.core.models import (  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
            Mode,
            ModeOverride,
        )

        self._loop("le-masked")
        Mode.objects.create(name="heads-down", entries={"le-masked": False})
        ModeOverride.objects.set_override("heads-down")
        assert loop_enabled("le-masked") is False

    def test_active_preset_force_on_admits_a_disabled_loop(self) -> None:
        from teatree.core.models import (  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
            Mode,
            ModeOverride,
        )

        self._loop("le-forced", enabled=False)
        Mode.objects.create(name="engaged", entries={"le-forced": True})
        ModeOverride.objects.set_override("engaged")
        assert loop_enabled("le-forced") is True

    def test_hold_beats_a_force_on_preset(self) -> None:
        from teatree.core.models import (  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
            Mode,
            ModeOverride,
        )

        self._loop("le-held-forced", enabled=False)
        LoopState.objects.disable("le-held-forced")
        Mode.objects.create(name="engaged", entries={"le-held-forced": True})
        ModeOverride.objects.set_override("engaged")
        assert loop_enabled("le-held-forced") is False


class TestLoopHeldFailsSafeButWarns(django.test.TestCase):
    """A per-loop PAUSE/DISABLE read error fails OPEN (no hold) — but WARNS, never whispers.

    The global kill-switch fails CLOSED on a read error; the symmetric per-loop
    hold fails OPEN so an unreadable DB can never silently disable a loop. That
    fail-open was swallowed at ``debug`` (#2584 / holistic 3c#5): a loop silently
    kept running with NO observable signal. It must log at WARNING so the operator
    can see the degraded read.
    """

    def test_read_error_returns_no_hold(self) -> None:
        with patch.object(LoopState.objects, "is_runnable", side_effect=RuntimeError("db down")):
            assert loop_held_in_db("review") is False

    def test_read_error_logs_at_warning(self) -> None:
        with (
            patch.object(LoopState.objects, "is_runnable", side_effect=RuntimeError("db down")),
            self.assertLogs("teatree.loop.loop_state_db", level="WARNING") as logs,
        ):
            loop_held_in_db("review")
        assert any("review" in line for line in logs.output)


class TestLoopEnabledFailsSafeButWarns(django.test.TestCase):
    """LP-8: ``loop_enabled``'s fail-open read error WARNS, symmetric with ``loop_held_in_db``.

    Both sibling reads fail OPEN (a hiccup never silently disables a loop), and the
    module's own doctrine (``loop_held_in_db``'s docstring) requires the swallow to
    be observable at WARNING — a loop silently mis-deciding is a real problem. The
    ``loop_enabled`` swallow logged at DEBUG, whispering the same class of degraded
    read its sibling shouts.
    """

    def test_read_error_returns_enabled(self) -> None:
        with patch.object(Loop.objects, "filter", side_effect=RuntimeError("db down")):
            assert loop_enabled("review") is True

    def test_read_error_logs_at_warning(self) -> None:
        with (
            patch.object(Loop.objects, "filter", side_effect=RuntimeError("db down")),
            self.assertLogs("teatree.loop.loop_state_db", level="WARNING") as logs,
        ):
            loop_enabled("review")
        assert any("review" in line for line in logs.output)


class TestLoopHeldInDbResolvesDbTier(django.test.TestCase):
    """``loop_held_in_db`` is the ``LoopState`` arm of the tick gate (#1913).

    An empty table holds no loop (the default); a ``PAUSED`` / ``DISABLED`` row
    holds it — including the core ``dispatch`` loop (the restart-surviving 'pause
    everything', 2026-06-03 incident); ``resume`` / ``enable`` clears the hold.
    """

    def test_empty_table_holds_no_loop(self) -> None:
        assert loop_held_in_db("review") is False

    def test_empty_table_holds_not_the_dispatch_loop(self) -> None:
        assert loop_held_in_db("dispatch") is False

    def test_pause_holds_a_loop(self) -> None:
        LoopState.objects.pause("review")
        assert loop_held_in_db("review") is True

    def test_disable_holds_a_loop(self) -> None:
        LoopState.objects.disable("review")
        assert loop_held_in_db("review") is True

    def test_pause_holds_the_dispatch_loop(self) -> None:
        LoopState.objects.pause("dispatch")
        assert loop_held_in_db("dispatch") is True

    def test_disable_holds_the_dispatch_loop(self) -> None:
        LoopState.objects.disable("dispatch")
        assert loop_held_in_db("dispatch") is True

    def test_resume_clears_the_hold(self) -> None:
        LoopState.objects.pause("review")
        LoopState.objects.resume("review")
        assert loop_held_in_db("review") is False

    def test_resume_clears_the_hold_on_the_dispatch_loop(self) -> None:
        LoopState.objects.pause("dispatch")
        LoopState.objects.resume("dispatch")
        assert loop_held_in_db("dispatch") is False
