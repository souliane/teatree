"""``interactive_claim_refusal`` — the headless lane's side of the runtime split (#1375 mirror).

``loop_dispatch_refusal`` stops the HEADLESS lane taking work the in-session
``/loop`` slot owns. This is the missing other half: under
``agent_runtime=headless`` an interactive session must not claim and hand-do the
phase work the headless factory owns.
"""

from django.test import TestCase

from teatree.core.headless_dispatch import has_registered_phase_agent, interactive_claim_refusal
from teatree.core.models import ConfigSetting, Session, Task, Ticket


class TestHasRegisteredPhaseAgent:
    """The registry lookup both refusals share — pure, ORM-free, so it needs no deferred import."""

    def test_a_dispatched_pair_is_registered(self) -> None:
        assert has_registered_phase_agent(role="author", phase="answering") is True

    def test_an_unregistered_pair_is_free_form(self) -> None:
        assert has_registered_phase_agent(role="author", phase="ad-hoc-phase") is False

    def test_it_agrees_with_the_orm_side_spelling(self) -> None:
        for role, phase in (("author", "answering"), ("author", "ad-hoc-phase"), ("reviewer", "answering")):
            assert has_registered_phase_agent(role=role, phase=phase) == Task.loop_dispatched(role=role, phase=phase)


class TestInteractiveClaimRefusal(TestCase):
    def _make_task(self, *, phase: str) -> Task:
        ticket = Ticket.objects.create(overlay="test")
        session = Session.objects.create(ticket=ticket, overlay="test", agent_id="agent-1")
        return Task.objects.create(ticket=ticket, session=session, phase=phase)

    def test_free_form_phase_is_never_refused(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "headless")
        assert interactive_claim_refusal(self._make_task(phase="ad-hoc-phase")) is None

    def test_registered_phase_is_claimable_under_interactive_runtime(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "interactive")
        assert interactive_claim_refusal(self._make_task(phase="answering")) is None

    def test_registered_phase_is_refused_under_headless_runtime(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "headless")
        reason = interactive_claim_refusal(self._make_task(phase="answering"))
        assert reason is not None
        assert "answering" in reason
        assert "agent_runtime" in reason

    def test_the_refusal_names_the_command_to_run_instead(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "headless")
        reason = interactive_claim_refusal(self._make_task(phase="answering"))
        assert reason is not None
        assert "work-next-headless" in reason

    def test_a_stale_interactive_row_is_still_refused(self) -> None:
        """The guard reads the LIVE setting, not the row's stored ``execution_target``.

        A phase task created while the runtime was ``interactive`` keeps
        ``execution_target=INTERACTIVE``; flipping the runtime to ``headless``
        must not leave it claimable in-session.
        """
        ConfigSetting.objects.set_value("agent_runtime", "interactive")
        task = self._make_task(phase="answering")
        assert task.execution_target == Task.ExecutionTarget.INTERACTIVE

        ConfigSetting.objects.set_value("agent_runtime", "headless")
        assert interactive_claim_refusal(task) is not None
