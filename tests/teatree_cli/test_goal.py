"""``t3 goal set/clear/list`` end-to-end through the CLI + standing_goal mgmt command (PR-25)."""

import json

from django.test import TestCase
from django_typer.management import TyperCommand
from typer.testing import CliRunner

from teatree.cli.goal import goal_app
from teatree.core.management.commands.standing_goal import Command
from teatree.core.models import StandingGoal

runner = CliRunner()


class TestGoalCli(TestCase):
    """Drive the real ``standing_goal`` management command through ``t3 goal``."""

    def test_set_registers_a_goal(self) -> None:
        result = runner.invoke(goal_app, ["set", "evals-green", "--check", "true"])
        assert result.exit_code == 0, result.output
        goal = StandingGoal.objects.get(name="evals-green")
        assert goal.check_command == "true"
        assert goal.active is True

    def test_set_empty_check_exits_nonzero(self) -> None:
        result = runner.invoke(goal_app, ["set", "evals-green", "--check", "   "])
        assert result.exit_code != 0
        assert StandingGoal.objects.count() == 0

    def test_list_shows_registered_goals(self) -> None:
        StandingGoal.objects.set_goal("evals-green", "true")
        result = runner.invoke(goal_app, ["list"])
        assert result.exit_code == 0, result.output
        assert "evals-green" in result.output

    def test_list_empty_is_a_noop_line(self) -> None:
        result = runner.invoke(goal_app, ["list"])
        assert result.exit_code == 0, result.output
        assert "no standing goals" in result.output.lower()

    def test_clear_named_goal_deletes_it(self) -> None:
        StandingGoal.objects.set_goal("a", "true")
        StandingGoal.objects.set_goal("b", "true")
        result = runner.invoke(goal_app, ["clear", "a"])
        assert result.exit_code == 0, result.output
        assert [g.name for g in StandingGoal.objects.all()] == ["b"]

    def test_clear_all_deletes_everything(self) -> None:
        StandingGoal.objects.set_goal("a", "true")
        StandingGoal.objects.set_goal("b", "true")
        result = runner.invoke(goal_app, ["clear"])
        assert result.exit_code == 0, result.output
        assert StandingGoal.objects.count() == 0

    def test_set_then_list_json_is_machine_readable(self) -> None:
        runner.invoke(goal_app, ["set", "evals-green", "--check", "true"])
        result = runner.invoke(goal_app, ["list", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["goals"][0]["name"] == "evals-green"

    def test_set_json_success_is_machine_readable(self) -> None:
        result = runner.invoke(goal_app, ["set", "evals-green", "--check", "true", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"ok": True, "name": "evals-green", "check_command": "true"}

    def test_set_json_error_on_empty_check(self) -> None:
        result = runner.invoke(goal_app, ["set", "evals-green", "--check", "   ", "--json"])
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["error"]

    def test_clear_json_reports_count_and_scope(self) -> None:
        StandingGoal.objects.set_goal("a", "true")
        result = runner.invoke(goal_app, ["clear", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"ok": True, "cleared": 1, "scope": "all standing goals"}

    def test_command_is_the_standing_goal_typer_command(self) -> None:
        assert issubclass(Command, TyperCommand)
        assert "standing" in Command.help.lower()
