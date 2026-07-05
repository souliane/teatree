"""A task stored with a short-verb phase dispatches like its canonical gerund.

Regression for the prompt.py phase-comparison sites (teatree integration audit
#20 family): ``task.phase == "coding"`` silently mis-fired for a task stored
with the short verb ``"code"``, dropping the phase-specific directive. Every
comparison now routes through ``normalize_phase``, so ``"code"`` and ``"coding"``
produce byte-identical output.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.agents import prompt
from teatree.agents.prompt import build_system_context, build_task_prompt
from teatree.core.models import Session, Task, Ticket


def _task(phase: str) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
    session = Session.objects.create(ticket=ticket, agent_id=phase)
    return Task.objects.create(ticket=ticket, session=session, phase=phase)


class TestBuildTaskPromptCodingAlias(TestCase):
    def test_short_verb_code_gets_the_coding_directive(self) -> None:
        # Pre-fix, ``task.phase == "coding"`` was False for ``"code"`` so the block was dropped.
        assert "PHASE: coding" in build_task_prompt(_task("code"))

    def test_canonical_gerund_gets_the_coding_directive(self) -> None:
        assert "PHASE: coding" in build_task_prompt(_task("coding"))


class TestPhaseSpecificLinesAlias(TestCase):
    def _lines(self, phase: str) -> tuple[str, ...]:
        return prompt._phase_specific_lines(_task(phase), [])

    def test_coding_short_verb_matches_gerund(self) -> None:
        lines = self._lines("code")
        assert "PHASE: coding — builder dispatch contract" in lines
        assert lines == self._lines("coding")

    def test_reviewing_short_verb_matches_gerund(self) -> None:
        lines = self._lines("review")
        assert "PHASE: reviewing" in lines
        assert lines == self._lines("reviewing")

    def test_shipping_short_verb_matches_gerund(self) -> None:
        assert self._lines("ship") == self._lines("shipping")

    def test_non_phase_is_empty(self) -> None:
        assert self._lines("scanning_news") == ()


class TestBuildSystemContextReviewScoping(TestCase):
    """The build_system_context skill-scoping branch routes the short verb too."""

    def _scoping_engaged(self, phase: str) -> bool:
        task = _task(phase)
        with patch.object(prompt, "_review_phase_scoping", return_value=(set(), set())) as scoping:
            build_system_context(task, skills=["t3:review"], lifecycle_skill="t3:review")
        return scoping.called

    def test_short_verb_review_engages_review_scoping(self) -> None:
        assert self._scoping_engaged("review")

    def test_canonical_reviewing_engages_review_scoping(self) -> None:
        assert self._scoping_engaged("reviewing")

    def test_coding_phase_does_not_engage_review_scoping(self) -> None:
        assert not self._scoping_engaged("code")
