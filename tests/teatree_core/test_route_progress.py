"""Which candidates of a skill's ordered route already ran on a task and failed through their own fault."""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt
from teatree.core.route_progress import failed_candidates
from tests.factories import planned_ticket

_LANDING = "landing_unverified: no new commit on feat-x — HEAD has not advanced past the base"
_CRASH = "Traceback (most recent call last):\n  File x.py\nRuntimeError: boom"
_PARKED = "limit_parked: subscription_session window"


class TestFailedCandidates(TestCase):
    def setUp(self) -> None:
        ticket = planned_ticket()
        self.task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))

    def _attempt(self, error: str, *, index: int | None = 0, skill: str = "code", exit_code: int = 0) -> TaskAttempt:
        return TaskAttempt.objects.create(
            task=self.task,
            ended_at=timezone.now(),
            exit_code=exit_code,
            error=error,
            route_candidate_index=index,
            route_source_skill=skill,
        )

    def test_a_refusal_and_a_crash_each_count_against_their_own_candidate(self) -> None:
        refusal = self._attempt(_LANDING, index=0)
        crash = self._attempt(_CRASH, index=1, exit_code=1)

        assert failed_candidates(self.task, "code") == {0: refusal.pk, 1: crash.pk}

    def test_the_latest_failure_of_a_candidate_names_the_attempt(self) -> None:
        self._attempt(_LANDING)
        latest = self._attempt(_LANDING)

        assert failed_candidates(self.task, "code") == {0: latest.pk}

    def test_a_failure_that_no_other_candidate_would_fix_does_not_count(self) -> None:
        self._attempt("missing required evidence for phase 'coding': result must include one of [files_modified]")
        self._attempt("agent_abandoned: the agent failed the task without a reason", index=1)
        self._attempt("outage_death: API Error 529", index=2)

        assert failed_candidates(self.task, "code") == {}

    def test_a_clean_attempt_and_an_unrouted_failure_do_not_count(self) -> None:
        self._attempt("")
        self._attempt(_LANDING, index=None)

        assert failed_candidates(self.task, "code") == {}

    def test_another_skills_route_is_not_this_routes_progress(self) -> None:
        self._attempt(_LANDING, skill="debug")

        assert failed_candidates(self.task, "code") == {}

    def test_a_park_resets_the_progress_made_before_it(self) -> None:
        self._attempt(_LANDING, index=0)
        self._attempt(_PARKED, index=None)
        after = self._attempt(_LANDING, index=1)

        assert failed_candidates(self.task, "code") == {1: after.pk}
