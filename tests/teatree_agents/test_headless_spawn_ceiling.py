"""``_build_options`` arms the sub-agent spawn ceiling on every headless dispatch.

Wiring it per-phase would need a phase list to keep in step with the delegation
grant; wiring it unconditionally cannot go stale — a phase that may not delegate
already carries ``Agent``/``Task`` in its disallow list, so the hook is inert there.
"""

from django.test import TestCase

from teatree.agents._headless_options import _build_options, resolve_spawn_ceiling
from teatree.agents.subagent_ceiling import DEFAULT_SPAWN_CEILING, SPAWN_TOOL_MATCHER
from teatree.config import UserSettings
from teatree.core.models import ConfigSetting, Session, Task, Ticket


class _Dispatch(TestCase):
    def _task(self, phase: str) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        return Task.objects.create(ticket=ticket, session=session, phase=phase)


class TestBuildOptionsArmsTheCeiling(_Dispatch):
    def test_a_delegating_phase_gets_the_pretooluse_matcher(self) -> None:
        options = _build_options(self._task("coding"), "ctx", phase="coding", skills=[])
        assert options.hooks is not None
        assert [m.matcher for m in options.hooks["PreToolUse"]] == [SPAWN_TOOL_MATCHER]

    def test_a_non_delegating_phase_is_armed_too_so_no_phase_can_slip_past(self) -> None:
        options = _build_options(self._task("shipping"), "ctx", phase="shipping", skills=[])
        assert options.hooks is not None
        assert options.hooks["PreToolUse"]

    def test_each_dispatch_gets_its_own_counter(self) -> None:
        first = _build_options(self._task("coding"), "ctx", phase="coding", skills=[])
        second = _build_options(self._task("coding"), "ctx", phase="coding", skills=[])
        assert first.hooks is not None
        assert second.hooks is not None
        assert first.hooks["PreToolUse"][0].hooks[0] is not second.hooks["PreToolUse"][0].hooks[0]

    def test_the_spawn_ceiling_does_not_move_the_turn_ceiling(self) -> None:
        # The spawn ceiling bounds fan-out; the TURN ceiling is its own DB-home
        # setting (``headless_max_turns``). Deriving one from the other would make a
        # fan-out retune silently change a run's turn budget.
        ConfigSetting.objects.set_value("subagent_spawn_ceiling", 3, scope="")
        options = _build_options(self._task("coding"), "ctx", phase="coding", skills=[])
        assert options.max_turns == UserSettings().headless_max_turns


class TestResolveSpawnCeiling(TestCase):
    def test_defaults_to_the_shipped_ceiling(self) -> None:
        assert resolve_spawn_ceiling() == DEFAULT_SPAWN_CEILING

    def test_an_operator_row_wins_over_the_shipped_default(self) -> None:
        ConfigSetting.objects.set_value("subagent_spawn_ceiling", 3, scope="")
        assert resolve_spawn_ceiling() == 3

    def test_zero_disables_the_gate(self) -> None:
        ConfigSetting.objects.set_value("subagent_spawn_ceiling", 0, scope="")
        assert resolve_spawn_ceiling() == 0
