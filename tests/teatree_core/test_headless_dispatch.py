"""The core → agents headless-runner inversion registry (#1922)."""

import pytest
from django.test import TestCase, override_settings

from teatree.core import headless_dispatch
from teatree.core.headless_dispatch import loop_dispatch_refusal
from teatree.core.models import Session, Task, Ticket


class TestLoopDispatchRefusal(TestCase):
    """``loop_dispatch_refusal`` — the single guard both headless entry points consult (#1375)."""

    def _make_task(self, *, phase: str) -> Task:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        # Force HEADLESS via an UPDATE so ``Task.save``'s insert-time
        # auto-route-to-interactive default does not re-fire.
        task.route_to_headless(reason="forced headless for the test")
        return task

    @override_settings(LOOP_ALLOW_HEADLESS_DISPATCH=False)
    def test_free_form_phase_is_not_refused(self) -> None:
        task = self._make_task(phase="free-form-phase")
        assert loop_dispatch_refusal(task) is None

    @override_settings(LOOP_ALLOW_HEADLESS_DISPATCH=False)
    def test_registered_phase_is_refused(self) -> None:
        task = self._make_task(phase="answering")
        reason = loop_dispatch_refusal(task)
        assert reason is not None
        assert "answering" in reason
        assert "LOOP_ALLOW_HEADLESS_DISPATCH" in reason

    @override_settings(LOOP_ALLOW_HEADLESS_DISPATCH=True)
    def test_registered_phase_allowed_when_override_on(self) -> None:
        task = self._make_task(phase="answering")
        assert loop_dispatch_refusal(task) is None


class TestHeadlessRunnerRegistry:
    def test_agents_ready_registers_the_real_runner(self) -> None:
        """``AgentsConfig.ready()`` ran at django.setup() — the runner resolves."""
        from teatree.agents.headless import run_headless  # noqa: PLC0415

        assert headless_dispatch.get_headless_runner() is run_headless

    def test_register_then_get_round_trips(self) -> None:
        def _fake_runner(task: object, *, phase: str, overlay_skill_metadata: object) -> object:
            return "attempt"

        original = headless_dispatch._runner
        try:
            headless_dispatch.register_headless_runner(_fake_runner)
            resolved = headless_dispatch.get_headless_runner()
            assert resolved(object(), phase="coding", overlay_skill_metadata={}) == "attempt"
        finally:
            headless_dispatch.register_headless_runner(original)

    def test_get_raises_when_unregistered(self) -> None:
        """Fail-LOUD: a dispatched headless task with no runner is fatal, never silent."""
        original = headless_dispatch._runner
        headless_dispatch._runner = None
        try:
            with pytest.raises(RuntimeError, match="no headless runner registered"):
                headless_dispatch.get_headless_runner()
        finally:
            headless_dispatch.register_headless_runner(original)
