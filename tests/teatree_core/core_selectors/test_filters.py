"""The shared overlay Q-builder (F1.6).

``managers.overlay_scope_q`` is the single source of truth for the Task overlay
clause; ``TaskQuerySet.for_overlay`` and the dashboard selector filters
(``selectors._filters``) all delegate to it, so the clause can never drift.
"""

from django.db.models import Q
from django.test import TestCase

from teatree.core.managers import overlay_scope_q
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.selectors._filters import _overlay_q, _task_overlay_q


class TestOverlayScopeQ(TestCase):
    @staticmethod
    def _task(overlay: str) -> Task:
        # Set BOTH the ticket and the session overlay so a non-empty overlay row
        # is not admitted by the empty-overlay clause on the other relation.
        ticket = Ticket.objects.create(overlay=overlay)
        session = Session.objects.create(ticket=ticket, agent_id="a", overlay=overlay)
        return Task.objects.create(ticket=ticket, session=session)

    def test_empty_overlay_yields_a_bare_q(self) -> None:
        assert overlay_scope_q(None) == Q()
        assert overlay_scope_q("") == Q()

    def test_scopes_tasks_by_overlay_and_admits_empty_rows(self) -> None:
        acme = self._task("acme")
        other = self._task("other")
        legacy = self._task("")

        matched = set(Task.objects.filter(overlay_scope_q("acme")).values_list("pk", flat=True))

        assert acme.pk in matched
        assert legacy.pk in matched  # pre-multi-overlay rows always admitted
        assert other.pk not in matched

    def test_matches_taskqueryset_for_overlay(self) -> None:
        self._task("acme")
        self._task("other")
        self._task("")

        via_builder = set(Task.objects.filter(overlay_scope_q("acme")).values_list("pk", flat=True))
        via_method = set(Task.objects.for_overlay("acme").values_list("pk", flat=True))

        assert via_builder == via_method

    def test_prefix_scopes_a_related_model(self) -> None:
        acme = self._task("acme")
        acme_attempt = TaskAttempt.objects.create(task=acme, execution_target="headless")
        other = self._task("other")
        TaskAttempt.objects.create(task=other, execution_target="headless")

        matched = set(TaskAttempt.objects.filter(overlay_scope_q("acme", prefix="task__")).values_list("pk", flat=True))

        assert acme_attempt.pk in matched
        assert len(matched) == 1


class TestFiltersDelegate(TestCase):
    """The selector filters are thin delegates to the shared builder."""

    def test_overlay_q_delegates(self) -> None:
        assert _overlay_q("acme") == overlay_scope_q("acme")
        assert _overlay_q("acme", prefix="task__") == overlay_scope_q("acme", prefix="task__")
        assert _overlay_q(None) == Q()

    def test_task_overlay_q_delegates_with_task_prefix(self) -> None:
        assert _task_overlay_q("acme") == overlay_scope_q("acme", prefix="task__")
        assert _task_overlay_q(None) == Q()
