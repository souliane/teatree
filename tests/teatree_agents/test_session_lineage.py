"""A headless Claude dispatch resumes only the session its task is TYPED to continue.

Which conversation that is comes off ``Task.session_continuation``; the per-shape cases
are pinned lane-for-lane in ``test_session_continuation``. What this module adds is the
whole ``_build_options`` path — the resolved option the SDK actually receives — and the
honesty escalation, which is resolved separately from that choice: a fresh reviewing
session still routes to the most-honest model when a session earlier in its lineage
escalated.
"""

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from teatree.agents._runner_options import _build_options
from teatree.agents.model_tiering import TIER_MODELS
from teatree.agents.prompt import build_system_context
from teatree.core.models import HonestyEscalation, Session, Task, TaskAttempt, Ticket
from teatree.core.models.task_handoff import schedule_resume

_PLANNING_SESSION = "11111111-1111-4111-8111-111111111111"
_CODING_SESSION = "22222222-2222-4222-8222-222222222222"
_TESTING_SESSION = "33333333-3333-4333-8333-333333333333"


@contextmanager
def _phase_models(models: dict[str, str]) -> Iterator[None]:
    db = Path(tempfile.mkdtemp()) / "db.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE teatree_config_setting (id INTEGER PRIMARY KEY, scope TEXT, key TEXT, value TEXT)")
    conn.execute(
        "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'agent_phase_models', ?)",
        (json.dumps(models),),
    )
    conn.commit()
    conn.close()
    with patch.dict(os.environ, {"T3_CONFIG_DB": str(db)}):
        yield


class _Lineage(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()

    def _task(self, phase: str, *, parent: Task | None = None, **kwargs: object) -> Task:
        session = Session.objects.create(ticket=self.ticket, agent_id=phase)
        return Task.objects.create(ticket=self.ticket, session=session, phase=phase, parent_task=parent, **kwargs)

    def _ran(
        self, phase: str, agent_session_id: str, *, parent: Task | None = None, result: dict | None = None
    ) -> Task:
        task = self._task(phase, parent=parent)
        TaskAttempt.objects.create(task=task, agent_session_id=agent_session_id, result=result or {})
        return task

    @staticmethod
    def _resume(task: Task) -> str | None:
        return _build_options(task, "ctx", phase=task.phase, skills=[]).resume


class TestAnUntypedChildStartsAFreshClaudeSession(_Lineage):
    """The default shape — a child nobody typed as a continuation — never inherits a session."""

    def test_coding_after_planning_does_not_resume_the_planning_session(self) -> None:
        coding = self._task("coding", parent=self._ran("planning", _PLANNING_SESSION))

        assert self._resume(coding) is None

    def test_testing_after_coding_does_not_resume_the_coding_session(self) -> None:
        testing = self.ticket.schedule_testing(parent_task=self._ran("coding", _CODING_SESSION))

        assert self._resume(testing) is None

    def test_reviewing_after_testing_does_not_resume_the_testing_session(self) -> None:
        reviewing = self.ticket.schedule_review(parent_task=self._ran("testing", _TESTING_SESSION))

        assert self._resume(reviewing) is None

    def test_a_same_phase_child_of_a_same_phase_child_stays_fresh(self) -> None:
        coding = self._task("coding", parent=self._ran("planning", _PLANNING_SESSION))

        assert self._resume(self._task("coding", parent=coding)) is None


class TestATypedContinuationResumes(_Lineage):
    def test_the_needs_input_resume_continues_the_parked_session(self) -> None:
        parked = self._ran(
            "coding", _CODING_SESSION, result={"needs_user_input": True, "user_input_reason": "Which DB?"}
        )

        assert self._resume(schedule_resume(parked, answer="postgres-1")) == _CODING_SESSION

    def test_a_retry_reopened_in_place_continues_its_own_attempt_session(self) -> None:
        retry = self._ran("coding", _CODING_SESSION, parent=self._ran("coding", _PLANNING_SESSION))
        retry.session_continuation = Task.SessionContinuation.SELF
        retry.save(update_fields=["session_continuation"])

        assert self._resume(retry) == _CODING_SESSION


class TestTheHonestyEscalationFollowsTheLineage(_Lineage):
    def setUp(self) -> None:
        super().setUp()
        self.planning = self._ran("planning", _PLANNING_SESSION)
        self.testing = self._ran(
            "testing", _TESTING_SESSION, parent=self._ran("coding", _CODING_SESSION, parent=self.planning)
        )
        self.reviewing = self.ticket.schedule_review(parent_task=self.testing)

    def _options(self):
        with _phase_models({"reviewing": "balanced"}):
            return _build_options(self.reviewing, "ctx", phase="reviewing", skills=[])

    def test_a_planning_escalation_raises_the_fresh_reviewing_session(self) -> None:
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=_PLANNING_SESSION)

        options = self._options()

        assert options.model == "opus"
        assert options.resume is None

    def test_a_sibling_task_scoped_escalation_does_not_bleed(self) -> None:
        sibling = self._task("reviewing", parent=self.testing)
        HonestyEscalation.record(HonestyEscalation.Reason.USER_ASKED, session_id=_PLANNING_SESSION, task_id=sibling.pk)

        assert self._options().model == TIER_MODELS["balanced"]

    def test_a_task_scoped_planning_escalation_raises_its_reviewing_descendant(self) -> None:
        HonestyEscalation.record(
            HonestyEscalation.Reason.USER_ASKED, session_id=_PLANNING_SESSION, task_id=self.planning.pk
        )

        assert self._options().model == "opus"

    def test_an_identically_scoped_sibling_lineage_does_not_raise_this_one(self) -> None:
        sibling_planning = self._ran("planning", _PLANNING_SESSION)
        HonestyEscalation.record(
            HonestyEscalation.Reason.USER_ASKED, session_id=_PLANNING_SESSION, task_id=sibling_planning.pk
        )

        assert self._options().model == TIER_MODELS["balanced"]


class TestAFreshPhaseSessionReceivesTheParentResult(_Lineage):
    def test_coding_after_planning_reads_the_planning_result_from_its_prompt(self) -> None:
        planning = self._ran("planning", _PLANNING_SESSION, result={"summary": "split the resume walk by phase"})
        coding = self._task("coding", parent=planning)

        assert self._resume(coding) is None
        assert "split the resume walk by phase" in build_system_context(coding, skills=[])
