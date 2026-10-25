"""Which candidates of a route already failed through their own fault, or had their read-only brief refused."""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt
from tests.factories import planned_ticket

_LANDING = "landing_unverified: no new commit on feat-x — HEAD has not advanced past the base"
_CRASH = "Traceback (most recent call last):\n  File x.py\nRuntimeError: boom"
_PARKED = "limit_parked: subscription_session window"
_REFUSED = "read-only executor brief refused: the issue body could not be read (RuntimeError)"


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

        assert self.task.attempts.failed_route_candidates("code") == {0: refusal.pk, 1: crash.pk}

    def test_a_refused_resume_counts_against_its_route_candidate(self) -> None:
        refusal = self._attempt(
            "result_error: context_exhausted - the resumed conversation ended without taking a turn"
        )

        assert self.task.attempts.failed_route_candidates("code") == {0: refusal.pk}

    def test_the_latest_failure_of_a_candidate_names_the_attempt(self) -> None:
        self._attempt(_LANDING)
        latest = self._attempt(_LANDING)

        assert self.task.attempts.failed_route_candidates("code") == {0: latest.pk}

    def test_a_failure_that_no_other_candidate_would_fix_does_not_count(self) -> None:
        self._attempt("missing required evidence for phase 'coding': result must include one of [files_modified]")
        self._attempt("agent_abandoned: the agent failed the task without a reason", index=1)
        self._attempt("outage_death: API Error 529", index=2)

        assert self.task.attempts.failed_route_candidates("code") == {}

    def test_a_clean_attempt_and_an_unrouted_failure_do_not_count(self) -> None:
        self._attempt("")
        self._attempt(_LANDING, index=None)

        assert self.task.attempts.failed_route_candidates("code") == {}

    def test_another_skills_route_is_not_this_routes_progress(self) -> None:
        self._attempt(_LANDING, skill="debug")

        assert self.task.attempts.failed_route_candidates("code") == {}

    def test_a_park_resets_the_progress_made_before_it(self) -> None:
        self._attempt(_LANDING, index=0)
        self._attempt(_PARKED, index=None)
        after = self._attempt(_LANDING, index=1)

        assert self.task.attempts.failed_route_candidates("code") == {1: after.pk}


class TestRefusedBriefCandidates(TestCase):
    def setUp(self) -> None:
        ticket = planned_ticket()
        self.task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket))

    def _attempt(self, error: str, *, index: int | None = 0, skill: str = "code") -> None:
        TaskAttempt.objects.create(
            task=self.task, ended_at=timezone.now(), error=error, route_candidate_index=index, route_source_skill=skill
        )

    def test_a_refusal_names_its_candidate_and_its_reason(self) -> None:
        self._attempt(_REFUSED, index=1)

        assert self.task.attempts.refused_brief_candidates("code") == {1: _REFUSED}

    def test_other_failures_unrouted_attempts_and_other_skills_do_not_count(self) -> None:
        self._attempt(_LANDING)
        self._attempt(_REFUSED, index=None)
        self._attempt(_REFUSED, skill="debug")

        assert self.task.attempts.refused_brief_candidates("code") == {}

    def test_a_park_lets_the_refused_candidate_run_again(self) -> None:
        self._attempt(_REFUSED, index=0)
        self._attempt(_PARKED, index=None)
        self._attempt(_REFUSED, index=1)

        assert self.task.attempts.refused_brief_candidates("code") == {1: _REFUSED}
