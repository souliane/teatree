"""``t3 eval ci-heal`` operator commands (#3201 PR-3a) — open / list / advance."""

import json

from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli.eval.ci_heal import ci_heal_app
from teatree.core.models import CiEvalHealSession


class TestOpen(TestCase):
    def test_open_creates_a_pending_session_and_prints_json(self) -> None:
        result = CliRunner().invoke(ci_heal_app, ["open", "--ref", "3201-feat-x"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["pr_ref"] == "3201-feat-x"
        assert payload["state"] == "pending"
        session = CiEvalHealSession.objects.get(pk=payload["id"])
        assert session.state == CiEvalHealSession.State.PENDING

    def test_open_default_max_fix_attempts_is_conservative(self) -> None:
        result = CliRunner().invoke(ci_heal_app, ["open", "--ref", "branch-y"])
        payload = json.loads(result.stdout)
        assert payload["max_fix_attempts"] == 2


class TestList(TestCase):
    def test_list_json_emits_the_sessions(self) -> None:
        CiEvalHealSession.objects.open_session(overlay="teatree", pr_ref="branch-a")
        result = CliRunner().invoke(ci_heal_app, ["list", "--json"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)
        assert [r["pr_ref"] for r in rows] == ["branch-a"]

    def test_list_empty_is_a_friendly_line(self) -> None:
        result = CliRunner().invoke(ci_heal_app, ["list"])
        assert result.exit_code == 0
        assert "no CI-eval heal sessions" in result.output


class TestAdvanceDryRun(TestCase):
    def test_advance_with_no_open_sessions_is_a_noop_line(self) -> None:
        result = CliRunner().invoke(ci_heal_app, ["advance"])
        assert result.exit_code == 0, result.output
        assert "no open CI-eval heal sessions" in result.output
