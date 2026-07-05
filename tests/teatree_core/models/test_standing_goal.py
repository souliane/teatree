"""Behaviour of the :class:`~teatree.core.models.standing_goal.StandingGoal` model (PR-25)."""

import pytest
from django.test import TestCase

from teatree.core.models import StandingGoal, StandingGoalError


class TestSetGoal(TestCase):
    def test_set_registers_an_active_goal(self) -> None:
        goal = StandingGoal.objects.set_goal("evals-green", "t3 eval status --green")
        assert goal.name == "evals-green"
        assert goal.check_command == "t3 eval status --green"
        assert goal.active is True

    def test_set_trims_whitespace(self) -> None:
        goal = StandingGoal.objects.set_goal("  evals-green  ", "  true  ")
        assert goal.name == "evals-green"
        assert goal.check_command == "true"

    def test_set_is_an_upsert_by_name(self) -> None:
        StandingGoal.objects.set_goal("evals-green", "old")
        StandingGoal.objects.set_goal("evals-green", "new")
        assert StandingGoal.objects.count() == 1
        assert StandingGoal.objects.get(name="evals-green").check_command == "new"

    def test_reset_re_arms_a_retired_goal(self) -> None:
        StandingGoal.objects.set_goal("evals-green", "true")
        StandingGoal.objects.retire("evals-green")
        StandingGoal.objects.set_goal("evals-green", "true")
        assert StandingGoal.objects.get(name="evals-green").active is True

    def test_empty_name_is_refused(self) -> None:
        with pytest.raises(StandingGoalError):
            StandingGoal.objects.set_goal("   ", "true")

    def test_empty_command_is_refused(self) -> None:
        with pytest.raises(StandingGoalError):
            StandingGoal.objects.set_goal("evals-green", "   ")


class TestActiveGoalsAndRetire(TestCase):
    def test_active_goals_excludes_retired(self) -> None:
        StandingGoal.objects.set_goal("a", "true")
        StandingGoal.objects.set_goal("b", "true")
        StandingGoal.objects.retire("a")
        names = [g.name for g in StandingGoal.objects.active_goals()]
        assert names == ["b"]

    def test_retire_is_single_use(self) -> None:
        StandingGoal.objects.set_goal("a", "true")
        assert StandingGoal.objects.retire("a") is True
        assert StandingGoal.objects.retire("a") is False

    def test_retire_absent_goal_is_false(self) -> None:
        assert StandingGoal.objects.retire("nope") is False


class TestClear(TestCase):
    def test_clear_one_deletes_only_it(self) -> None:
        StandingGoal.objects.set_goal("a", "true")
        StandingGoal.objects.set_goal("b", "true")
        assert StandingGoal.objects.clear("a") == 1
        assert [g.name for g in StandingGoal.objects.all()] == ["b"]

    def test_clear_all_deletes_everything(self) -> None:
        StandingGoal.objects.set_goal("a", "true")
        StandingGoal.objects.set_goal("b", "true")
        assert StandingGoal.objects.clear() == 2
        assert StandingGoal.objects.count() == 0
