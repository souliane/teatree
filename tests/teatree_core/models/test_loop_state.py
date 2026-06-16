"""DB-backed per-loop enable/disable/pause/resume state machine (#1913).

Integration-first against the real DB. ``LoopState`` is the canonical control
tier for the loop control plane (mirrors ``ConfigSetting`` / ``MergeClear`` —
"canonical tier is the DB with file/env fallback"): an absent row resolves to
the ``ENABLED`` default so an empty table is a provable no-op, and a present row
carries the durable enabled / paused / disabled status that survives a restart.

The transitions are atomic single-row upserts so two racing writers cannot
produce a duplicate row, and they are idempotent — re-issuing the same
transition is a no-op that leaves the one row in the target status.
"""

from django.test import TestCase

from teatree.core.models import LoopState, LoopStatus


class TestLoopStateDefault(TestCase):
    def test_status_of_absent_loop_is_enabled(self) -> None:
        # Empty table -> the ENABLED default; never an exception.
        assert LoopState.objects.status_of("review") is LoopStatus.ENABLED

    def test_absent_loop_is_runnable_and_not_paused_not_disabled(self) -> None:
        assert LoopState.objects.is_runnable("review") is True
        assert LoopState.objects.is_paused("review") is False
        assert LoopState.objects.is_disabled("review") is False


class TestLoopStateTransitions(TestCase):
    def test_pause_then_status_is_paused(self) -> None:
        LoopState.objects.pause("review")
        assert LoopState.objects.status_of("review") is LoopStatus.PAUSED
        assert LoopState.objects.is_paused("review") is True
        assert LoopState.objects.is_runnable("review") is False

    def test_disable_then_status_is_disabled(self) -> None:
        LoopState.objects.disable("ship")
        assert LoopState.objects.status_of("ship") is LoopStatus.DISABLED
        assert LoopState.objects.is_disabled("ship") is True
        assert LoopState.objects.is_runnable("ship") is False

    def test_resume_from_paused_returns_to_enabled(self) -> None:
        LoopState.objects.pause("review")
        LoopState.objects.resume("review")
        assert LoopState.objects.status_of("review") is LoopStatus.ENABLED
        assert LoopState.objects.is_runnable("review") is True

    def test_enable_from_disabled_returns_to_enabled(self) -> None:
        LoopState.objects.disable("ship")
        LoopState.objects.enable("ship")
        assert LoopState.objects.status_of("ship") is LoopStatus.ENABLED
        assert LoopState.objects.is_runnable("ship") is True

    def test_resume_clears_a_disabled_state_too(self) -> None:
        # resume / enable are the same return-to-ENABLED transition: a single
        # "make it run again" verb must clear EITHER hold so a disabled loop is
        # never stuck because the operator used the pause-vocabulary verb.
        LoopState.objects.disable("ship")
        LoopState.objects.resume("ship")
        assert LoopState.objects.status_of("ship") is LoopStatus.ENABLED

    def test_disable_overrides_a_pause(self) -> None:
        LoopState.objects.pause("review")
        LoopState.objects.disable("review")
        assert LoopState.objects.status_of("review") is LoopStatus.DISABLED


class TestLoopStateAtomicityAndIdempotence(TestCase):
    def test_transition_keeps_exactly_one_row_per_name(self) -> None:
        LoopState.objects.pause("review")
        LoopState.objects.disable("review")
        LoopState.objects.enable("review")
        assert LoopState.objects.filter(name="review").count() == 1

    def test_pause_is_idempotent(self) -> None:
        LoopState.objects.pause("review")
        LoopState.objects.pause("review")
        assert LoopState.objects.filter(name="review", status=LoopStatus.PAUSED).count() == 1

    def test_enable_absent_loop_is_a_noop_no_row_needed(self) -> None:
        # Enabling an already-(implicitly)-enabled loop must not error and must
        # not need a row — the default IS enabled.
        LoopState.objects.enable("never-touched")
        assert LoopState.objects.status_of("never-touched") is LoopStatus.ENABLED


class TestLoopStateRestartSurvival(TestCase):
    def test_paused_state_survives_a_fresh_read(self) -> None:
        # The 2026-06-03 'pause everything' incident: a paused state must be
        # durable, not in-memory — a fresh manager read (a new process /
        # restart) sees the same PAUSED status.
        LoopState.objects.pause("dispatch")
        reread = LoopState.objects.status_of("dispatch")
        assert reread is LoopStatus.PAUSED


class TestLoopStateStr(TestCase):
    def test_str_is_informative(self) -> None:
        row = LoopState.objects.pause("review")
        assert "review" in str(row)
        assert "paused" in str(row).lower()
